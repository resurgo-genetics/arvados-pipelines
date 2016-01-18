#!/usr/bin/env python

import os           # Import the os module for basic path manipulation
import arvados      # Import the Arvados sdk module
import re
import subprocess
import jinja2
from select import select
from signal import signal, SIGINT, SIGTERM, SIGKILL
from time import sleep

# the amount to weight each sequence contig
weight_seq = 6000

# list of process ids of all children
child_pids = []

# list of fds on which to watch for and process output
watch_fds = []

# dict mapping from fd to the text to tag the output with
watch_fd_tags = dict()

# fifos to delete after processing is done
fifos_to_delete = []

class InvalidArgumentError(Exception):
    pass

class FileAccessError(Exception):
    pass

def one_task_per_cram_file(if_sequence=0, and_end_task=True, 
                           skip_sq_sn_regex='_decoy$', 
                           genome_chunks=200):
    """
    Queue one task for each cram file in this job's input collection.
    Each new task will have an "input" parameter: a manifest
    containing one .cram file and its corresponding .crai index file.
    Files in the input collection that are not named *.cram or *.crai
    (as well as *.crai files that do not match any .cram file present)
    are silently ignored.
    if_sequence and and_end_task arguments have the same significance
    as in arvados.job_setup.one_task_per_input_file().
    """
    if if_sequence != arvados.current_task()['sequence']:
        return

    skip_sq_sn_r = re.compile(skip_sq_sn_regex)

    # Ensure we have a .fa reference file with corresponding .fai index and .dict
    reference_coll = arvados.current_job()['script_parameters']['reference_collection']
    rcr = arvados.CollectionReader(reference_coll)
    ref_fasta = {}
    ref_fai = {}
    ref_dict = {}
    ref_input = None
    dict_reader = None
    for rs in rcr.all_streams():
        for rf in rs.all_files():
            if re.search(r'\.fa$', rf.name()):
                ref_fasta[rs.name(), rf.name()] = rf
            elif re.search(r'\.fai$', rf.name()):
                ref_fai[rs.name(), rf.name()] = rf
            elif re.search(r'\.dict$', rf.name()):
                ref_dict[rs.name(), rf.name()] = rf
    for ((s_name, f_name), fasta_f) in ref_fasta.items():
        fai_f = ref_fai.get((s_name, re.sub(r'fa$', 'fai', f_name)), 
                            ref_fai.get((s_name, re.sub(r'fa$', 'fa.fai', f_name)), 
                                        None))
        dict_f = ref_dict.get((s_name, re.sub(r'fa$', 'dict', f_name)), 
                              ref_dict.get((s_name, re.sub(r'fa$', 'fa.dict', f_name)), 
                                           None))
        if fasta_f and fai_f and dict_f:
            # found a set of all three! 
            ref_input = fasta_f.as_manifest()
            ref_input += fai_f.as_manifest()
            ref_input += dict_f.as_manifest()
            dict_reader = dict_f
            break
    if ref_input is None:
        raise InvalidArgumentError("Expected a reference fasta with fai and dict in reference_collection. Found [%s]" % ' '.join(rf.name() for rf in rs.all_files()))
    if dict_reader is None:
        raise InvalidArgumentError("Could not find .dict file in reference_collection. Found [%s]" % ' '.join(rf.name() for rf in rs.all_files()))

    # Create a portable data hash for the ref_input manifest
    try:
        r = arvados.api().collections().create(body={"manifest_text": ref_input}).execute()
        ref_input_pdh = r["portable_data_hash"]
    except:
        raise 

    # Load the dict data
    interval_header = ""
    dict_lines = dict_reader.readlines()
    dict_header = dict_lines.pop(0)
    if re.search(r'^@HD', dict_header) is None:
        raise InvalidArgumentError("Dict file in reference collection does not have correct header: [%s]" % dict_header)
    interval_header += dict_header
    print "Dict header is %s" % dict_header
    sn_intervals = dict()
    sns = []
    total_len = 0
    for sq in dict_lines:
        if re.search(r'^@SQ', sq) is None:
            raise InvalidArgumentError("Dict file contains malformed SQ line: [%s]" % sq)
        interval_header += sq
        sn = None
        ln = None
        for tagval in sq.split("\t"):
            tv = tagval.split(":", 1)
            if tv[0] == "SN":
                sn = tv[1]
            if tv[0] == "LN":
                ln = tv[1]
            if sn and ln:
                break
        if not (sn and ln):
            raise InvalidArgumentError("Dict file SQ entry missing required SN and/or LN parameters: [%s]" % sq)
        assert(sn and ln)
        if sn_intervals.has_key(sn):
            raise InvalidArgumentError("Dict file has duplicate SQ entry for SN %s: [%s]" % (sn, sq))
        if skip_sq_sn_r.search(sn):
            next
        sn_intervals[sn] = (1, int(ln))
        sns.append(sn)
        total_len += int(ln)
    total_sequences = len(sns)

    # Chunk the genome into genome_chunks pieces
    # weighted by both number of base pairs and number of seqs
    print "Total sequences included: %s" % (total_sequences)
    print "Total genome length: %s" % (total_len)
    total_points = total_len + (total_sequences * weight_seq)
    chunk_points = int(total_points / genome_chunks)
    chunk_input_pdh_name = []
    print "Chunking genome into %s chunks of ~%s points" % (genome_chunks, chunk_points)
    for chunk_i in range(0, genome_chunks):
        chunk_num = chunk_i + 1
        chunk_intervals_count = 0
        chunk_input_name = dict_reader.name() + (".%s_of_%s.region_list" % (chunk_num, genome_chunks))
        print "Creating interval file for chunk %s" % chunk_num
        chunk_c = arvados.collection.CollectionWriter(num_retries=3)
        chunk_c.start_new_file(newfilename=chunk_input_name)
        # chunk_c.write(interval_header)
        remaining_points = chunk_points
        while len(sns) > 0:
            sn = sns.pop(0)
            remaining_points -= weight_seq
            if remaining_points <= 0:
                sns.insert(0, sn)
                break
            if not sn_intervals.has_key(sn):
                raise ValueError("sn_intervals missing entry for sn [%s]" % sn)
            start, end = sn_intervals[sn]
            if (end-start+1) > remaining_points:
                # not enough space for the whole sq, split it
                real_end = end
                end = remaining_points + start - 1
                assert((end-start+1) <= remaining_points)
                sn_intervals[sn] = (end+1, real_end)
                sns.insert(0, sn)
            #interval = "%s\t%s\t%s\t+\t%s\n" % (sn, start, end, "interval_%s_of_%s_%s" % (chunk_num, genome_chunks, sn))
            interval = "%s\t%s\t%s\n" % (sn, start, end)
            remaining_points -= (end-start+1)
            chunk_c.write(interval)
            chunk_intervals_count += 1
            if remaining_points <= 0:
                break
        if chunk_intervals_count > 0:
            chunk_input_pdh = chunk_c.finish()
            print "Chunk intervals file %s saved as %s" % (chunk_input_name, chunk_input_pdh)
            chunk_input_pdh_name.append((chunk_input_pdh, chunk_input_name))
        else:
            print "WARNING: skipping empty intervals for %s" % chunk_input_name
    print "Have %s chunk collections: [%s]" % (len(chunk_input_pdh_name), ' '.join([x[0] for x in chunk_input_pdh_name]))

    # prepare CRAM input collections
    job_input = arvados.current_job()['script_parameters']['inputs_collection']
    cr = arvados.CollectionReader(job_input)
    cram = {}
    crai = {}
    for s in cr.all_streams():
        for f in s.all_files():
            if re.search(r'\.cram$', f.name()):
                cram[s.name(), f.name()] = f
            elif re.search(r'\.crai$', f.name()):
                crai[s.name(), f.name()] = f
    for ((s_name, f_name), cram_f) in cram.items():
        crai_f = crai.get((s_name, re.sub(r'cram$', 'crai', f_name)), 
                          crai.get((s_name, re.sub(r'cram$', 'cram.crai', f_name)), 
                                   None))
        task_input = cram_f.as_manifest()
        if crai_f:
            task_input += crai_f.as_manifest()
        else:
            # no CRAI for CRAM
            raise InvalidArgumentError("No correponding CRAI file found for CRAM file %s" % f_name)

        # Create a portable data hash for the task's subcollection
        try:
            r = arvados.api().collections().create(body={"manifest_text": task_input}).execute()
            task_input_pdh = r["portable_data_hash"]
        except:
            raise 
        
        for chunk_input_pdh, chunk_input_name in chunk_input_pdh_name:
            # Create task for each CRAM / chunk
            print "Creating new task to process %s with chunk interval %s " % (f_name, chunk_input_name)
            new_task_attrs = {
                'job_uuid': arvados.current_job()['uuid'],
                'created_by_job_task_uuid': arvados.current_task()['uuid'],
                'sequence': if_sequence + 1,
                'parameters': {
                    'input': task_input_pdh,
                    'ref': ref_input_pdh,
                    'chunk': chunk_input_pdh
                    }
                }
            arvados.api().job_tasks().create(body=new_task_attrs).execute()

    if and_end_task:
        print "Ending task 0 successfully"
        arvados.api().job_tasks().update(uuid=arvados.current_task()['uuid'],
                                         body={'success':True}
                                         ).execute()
        exit(0)

def sigint_handler(signum, frame):
    print "sigint_handler received signal %s" % signum
    # pass signal along to children
    for pid in child_pids:
        os.kill(pid, SIGINT)

def sigterm_handler(signum, frame):
    print "sigterm_handler received signal %s" % signum
    # pass signal along to children
    for pid in child_pids:
        os.kill(pid, SIGTERM)
    # also try to SIGKILL children
    for pid in child_pids:
        os.kill(pid, SIGKILL)

def run_child_cmd(cmd, stdin=None, stdout=None, tag="child command"):
    print "Running %s" % cmd
    try:
        p = subprocess.Popen(cmd,
                             stdin=stdin,
                             stdout=stdout,
                             stderr=subprocess.PIPE,
                             close_fds=True,
                             shell=False)
    except Exception as e:
        print "Error running %s: [%s] running %s" % (tag, e, bcftools_mpileup_cmd)
        raise
    child_pids.append(p.pid)
    watch_fds.append(p.stderr)
    watch_fd_tags[p.stderr] = tag
    return p

def close_process_if_finished(p, tag="", close_fds=[], close_files=[], ignore_error=False):
    if p and p.poll() is not None:
        # process has finished
        exitval = p.wait()
        if (not ignore_error) and (exitval != 0):
            print "WARNING: %s exited with exit code %s" % (tag, exitval)
            raise Exception("%s exited with exit code %s" % (tag, exitval))
        print "%s completed successfully" % (tag)
        child_pids.remove(p.pid)
        watch_fds.remove(p.stderr)
        del watch_fd_tags[p.stderr]
        for fd in close_fds:
            os.close(fd)
        for f in close_files:
            f.close()
        return None
    else:
        return p
    # elif p:
    #     print "process %s (%s) still running" % (tag, p.pid)
    #     return p
    # else:
    #     print "process %s is gone" % (tag)
    #     return p

def watch_fds_and_print_output():
    # check for any output to be read and print it
    ready_fds = select(watch_fds, [], [], 0)[0]
    for fd in ready_fds: 
        tag = watch_fd_tags[fd]
        line = fd.readline()
        if line:
            print "%s: %s" % (tag, line.rstrip())

def main():
    signal(SIGINT, sigint_handler)
    signal(SIGTERM, sigterm_handler)
    
    this_job = arvados.current_job()
    
    skip_sq_sn_regex = this_job['script_parameters']['skip_sq_sn_regex']

    genome_chunks = int(this_job['script_parameters']['genome_chunks'])
    if genome_chunks < 1:
        raise InvalidArgumentError("genome_chunks must be a positive integer")

    # Setup sub tasks 1-N (and terminate if this is task 0)
    one_task_per_cram_file(if_sequence=0, and_end_task=True, 
                           skip_sq_sn_regex=skip_sq_sn_regex, 
                           genome_chunks=genome_chunks)

    # Get object representing the current task
    this_task = arvados.current_task()

    # We will never reach this point if we are in the 0th task
    assert(this_task['sequence'] != 0)

    # Get reference FASTA
    ref_file = None
    print "Mounting reference FASTA collection"
    ref_dir = arvados.get_task_param_mount('ref')

    for f in arvados.util.listdir_recursive(ref_dir):
        if re.search(r'\.fa$', f):
            ref_file = os.path.join(ref_dir, f)
    if ref_file is None:
        raise InvalidArgumentError("No reference fasta found in reference collection.")
    # Ensure we can read the reference file
    if not os.access(ref_file, os.R_OK):
        raise FileAccessError("reference FASTA file not readable: %s" % ref_file)
    # TODO: could check readability of .fai and .dict as well?

    # Get genome chunk intervals file
    chunk_file = None
    print "Mounting chunk collection"
    chunk_dir = arvados.get_task_param_mount('chunk')

    for f in arvados.util.listdir_recursive(chunk_dir):
        if re.search(r'\.region_list$', f):
            chunk_file = os.path.join(chunk_dir, f)
    if chunk_file is None:
        raise InvalidArgumentError("No chunk intervals file found in chunk collection.")
    # Ensure we can read the chunk file
    if not os.access(chunk_file, os.R_OK):
        raise FileAccessError("Chunk intervals file not readable: %s" % chunk_file)

    # Get single CRAM file for this task 
    input_dir = None
    print "Mounting task input collection"
    input_dir = arvados.get_task_param_mount('input')

    input_cram_files = []
    for f in arvados.util.listdir_recursive(input_dir):
        if re.search(r'\.cram$', f):
            stream_name, input_file_name = os.path.split(f)
            input_cram_files += [os.path.join(input_dir, f)]
    if len(input_cram_files) != 1:
        raise InvalidArgumentError("Expected exactly one cram file per task.")

    # There is only one CRAM file
    cram_file = input_cram_files[0]

    # Ensure we can read the CRAM file
    if not os.access(cram_file, os.R_OK):
        raise FileAccessError("CRAM file not readable: %s" % cram_file)

    # Ensure we have corresponding CRAI index and can read it as well
    cram_file_base, cram_file_ext = os.path.splitext(cram_file)
    assert(cram_file_ext == ".cram")
    crai_file = cram_file_base + ".crai"
    if not os.access(crai_file, os.R_OK):
        crai_file = cram_file_base + ".cram.crai"
        if not os.access(crai_file, os.R_OK):
            raise FileAccessError("No readable CRAM index file for CRAM file: %s" % cram_file)

    # Will write to out_dir, make sure it is empty
    tmp_dir = arvados.current_task().tmpdir
    out_dir = os.path.join(tmp_dir, 'out')
    if os.path.exists(out_dir):
        old_out_dir = out_dir + ".old"
        print "Moving out_dir %s out of the way (to %s)" % (out_dir, old_out_dir) 
        try:
            os.rename(out_dir, old_out_dir)
        except:
            raise
    try:
        os.mkdir(out_dir)
        os.chdir(out_dir)
    except:
        raise
    output_basename = os.path.basename(cram_file_base) + "." + os.path.basename(chunk_file)
    out_file_tmp = os.path.join(tmp_dir, output_basename + ".g.vcf.tmp")
    penultimate_out_file = os.path.join(tmp_dir, output_basename + ".provheader.g.vcf.gz")
    final_out_file = os.path.join(out_dir, output_basename + ".g.vcf.gz")

#    bash_cmd_pipe = "samtools view -h -u -@ 1 -T %s %s | bcftools mpileup -t AD,INFO/AD -C50 -pm2 -F0.1 -d10000 --gvcf 1,2,3,4,5,10,15 -f %s -Ou - | bcftools view  -Ou | bcftools norm -f %s -Ob -o %s" % (ref_file, cram_file, ref_file, ref_file, out_file)
    regions = []
    print "Preparing region list from chunk file [%s]" % chunk_file
    with open(chunk_file, 'r') as f:
        for line in f.readlines():
            (chr, start, end) = line.rstrip().split()
            region = "%s:%s-%s" % (chr, start, end)
            regions.append(region)
    total_region_count = len(regions)

    print "Preparing fifos for output from %s bcftools mpileup commands (one for each region) to bcftools concat" % total_region_count

    concat_noheader_fifos = dict()
    concat_headeronly_tmps = dict()
    current_region_num = 0
    for region in regions:
        current_region_num += 1
        concat_noheader_fifo = os.path.join(tmp_dir, output_basename + (".part_%s_of_%s.g.vcf" % (current_region_num, total_region_count)))
        try:
            os.mkfifo(concat_noheader_fifo, 0600)
        except:
            print "ERROR: could not mkfifo %s" % concat_noheader_fifo
            raise
        fifos_to_delete.append(concat_noheader_fifo)
        concat_noheader_fifos[region] = concat_noheader_fifo
        concat_headeronly_tmp = os.path.join(tmp_dir, output_basename + (".part_%s_of_%s.headeronly.g.vcf.gz" % (current_region_num, total_region_count)))
        concat_headeronly_tmps[region] = concat_headeronly_tmp

    region_concat_cmd = ["cat"]
    region_concat_cmd.extend([concat_noheader_fifos[region] for region in regions])

    # open file for output file
    out_file_tmp_f = open(out_file_tmp, 'wb')
    
    region_concat_p = run_child_cmd(region_concat_cmd,
                                    stdout=out_file_tmp_f,
                                    tag="bcftools concat (stderr)")
    
    current_region_num = 0
    current_concat_noheader_fifo_f = None
    regions_to_process = list(regions)
    bcftools_mpileup_p = None
    bcftools_norm_p = None
    part_tee_p = None
    bcftools_view_headeronly_p = None
    bcftools_view_noheader_p = None
    while True:
        # at least one of the regional aggregation processes is still running

        watch_fds_and_print_output()
    
        if (
                (bcftools_mpileup_p is None) and
                (bcftools_norm_p is None) and 
                (part_tee_p is None) and
                (bcftools_view_headeronly_p is None) and 
                (bcftools_view_noheader_p is None)
        ):
            # no per-region processes are running (they have finished or 
            # have not yet started)
            if len(regions_to_process) > 0:
                # have more regions to run
                region = regions_to_process.pop(0)
                current_region_num += 1
                region_label = "%s/%s [%s]" % (current_region_num, total_region_count, region)
                concat_noheader_fifo = concat_noheader_fifos[region]
                bcftools_view_noheader_input_fifo = os.path.join(tmp_dir, output_basename + (".part_%s_of_%s.noheader.g.bcf" % (current_region_num, total_region_count)))
                part_tee_cmd = ["tee", bcftools_view_noheader_input_fifo]
                bcftools_view_noheader_cmd = ["bcftools", "view", "-H", "-Ov", bcftools_view_noheader_input_fifo]
                concat_headeronly_tmp = concat_headeronly_tmps[region]
                bcftools_view_headeronly_cmd = ["bcftools", "view", "-h", "-Oz", "-o", concat_headeronly_tmp]
                bcftools_norm_cmd = ["bcftools", "norm", 
                                     "-f", ref_file, 
                                     "-Ou"]
                bcftools_mpileup_cmd = ["bcftools", "mpileup",
                                        "-t", "AD,INFO/AD",
                                        "-C50", 
                                        "-pm2", 
                                        "-F0.1",
                                        "-d10000",
                                        "--gvcf", "1,2,3,4,5,10,15",
                                        "-f", ref_file,
                                        "-Ou",
                                        "-r", region,
                                        cram_file]

                print "Creating 'bcftools mpileup | bcftools norm' pipe for region %s" % (region_label)
                bcftools_norm_stdin_pipe_read, bcftools_norm_stdin_pipe_write = os.pipe()

                print "Creating 'bcftools norm | tee' pipe for region %s" % (region_label)
                part_tee_stdin_pipe_read, part_tee_stdin_pipe_write = os.pipe()
                
                print "Creating 'tee | bcftools view -h' pipe for region %s" % (region_label)
                bcftools_view_headeronly_stdin_pipe_read, bcftools_view_headeronly_stdin_pipe_write = os.pipe()
                
                print "Creating 'tee | bcftools view' named pipe [%s] for region %s" % (bcftools_view_noheader_input_fifo, region_label)
                try:
                    os.mkfifo(bcftools_view_noheader_input_fifo, 0600)
                except:
                    print "ERROR: could not mkfifo %s" % bcftools_view_noheader_input_fifo
                    raise
                fifos_to_delete.append(bcftools_view_noheader_input_fifo)

                print "Opening concat fifo %s for writing" % concat_noheader_fifo
                if current_concat_noheader_fifo_f is not None:
                    #print "ERROR: current_concat_noheader_fifo_f was not closed properly"
                    #raise Exception("current_concat_noheader_fifo_f was not closed properly")
                    current_concat_noheader_fifo_f.close()
                current_concat_noheader_fifo_f = open(concat_noheader_fifo, 'wb')

                bcftools_mpileup_p = run_child_cmd(bcftools_mpileup_cmd,
                                                   stdout=bcftools_norm_stdin_pipe_write,
                                                   tag="bcftools mpileup %s" % (region_label))
                
                bcftools_norm_p = run_child_cmd(bcftools_norm_cmd,
                                                stdin=bcftools_norm_stdin_pipe_read,
                                                stdout=part_tee_stdin_pipe_write,
                                                tag="bcftools norm %s" % (region_label))

                part_tee_p = run_child_cmd(part_tee_cmd,
                                           stdin=part_tee_stdin_pipe_read,
                                           stdout=bcftools_view_headeronly_stdin_pipe_write,
                                           tag="tee %s" % (region_label))
                
                bcftools_view_headeronly_p = run_child_cmd(bcftools_view_headeronly_cmd,
                                                           stdin=bcftools_view_headeronly_stdin_pipe_read,
                                                           tag="bcftools view -h %s" % (region_label))

                bcftools_view_noheader_p = run_child_cmd(bcftools_view_noheader_cmd,
                                                         stdout=current_concat_noheader_fifo_f,
                                                         tag="bcftools view %s" % (region_label))

        bcftools_mpileup_p = close_process_if_finished(bcftools_mpileup_p,
                                                       "bcftools mpileup %s" % (region_label),
                                                       close_fds=[bcftools_norm_stdin_pipe_write])

        bcftools_norm_p = close_process_if_finished(bcftools_norm_p,
                                                    "bcftools norm %s" % (region_label),
                                                    close_fds=[bcftools_norm_stdin_pipe_read, 
                                                               part_tee_stdin_pipe_write])
        
        part_tee_p = close_process_if_finished(part_tee_p,
                                               "tee %s" % (region_label),
                                               close_fds=[part_tee_stdin_pipe_read,
                                                          bcftools_view_headeronly_stdin_pipe_write],
                                               ignore_error=True)

        bcftools_view_headeronly_p = close_process_if_finished(bcftools_view_headeronly_p,
                                                               "bcftools view -h %s" % (region_label),
                                                               close_fds=[bcftools_view_headeronly_stdin_pipe_read])

        bcftools_view_noheader_p = close_process_if_finished(bcftools_view_noheader_p,
                                                             "bcftools view %s" % (region_label),
                                                             close_files=[current_concat_noheader_fifo_f])

        region_concat_p = close_process_if_finished(region_concat_p,
                                                      "bcftools concat",
                                                      close_files=[out_file_tmp_f])

        # end loop once all processes have finished
        if not (
                (region_concat_p and region_concat_p.poll() is None)
                or (bcftools_view_noheader_p and bcftools_view_noheader_p.poll() is None)
                or (bcftools_view_headeronly_p and bcftools_view_headeronly_p.poll() is None)
                or (part_tee_p and part_tee_p.poll() is None)
                or (bcftools_norm_p and bcftools_norm_p.poll() is None)
                or (bcftools_mpileup_p and bcftools_mpileup_p.poll() is None)
        ):
            print "All region work has completed"
            break
        else:
            sleep(0.01)
            # continue to next loop iteration


    if len(child_pids) > 0:
        print "WARNING: some children are still alive: [%s]" % (child_pids)
        for pid in child_pids:
            print "Attempting to terminate %s forcefully" % (pid)
            try:
                os.kill(pid, SIGTERM)
            except Exception as e:
                print "Could not kill pid %s: %s" % (pid, e)

    for fifo in fifos_to_delete:
        try:
            os.remove(fifo)
        except:
            raise

    concat_headeronly_tmp_fofn = os.path.join(tmp_dir, output_basename + ".fifos_fofn")
    tmp_files_to_delete = []
    print "Preparing fofn for bcftools concat (headeronly): %s" % (concat_headeronly_tmp_fofn)
    with open(concat_headeronly_tmp_fofn, 'w') as f:
        print "Checking files for regions: %s" % regions
        for concat_headeronly_tmp in [concat_headeronly_tmps[region] for region in regions]:
            if os.path.exists(concat_headeronly_tmp):
                print "Adding %s to fofn" % concat_headeronly_tmp
                f.write("%s\n" % concat_headeronly_tmp)
                tmp_files_to_delete.append(concat_headeronly_tmp)
            else:
                print "WARNING: no output file for %s (there was probably no data in the region)" % concat_headeronly_tmp

    final_headeronly_tmp = os.path.join(tmp_dir, output_basename + ".headeronly.g.vcf")
    final_headeronly_tmp_f = open(final_headeronly_tmp, 'wb')

    print "Creating 'bcftools concat | grep' pipe" 
    grep_headeronly_stdin_pipe_read, grep_headeronly_stdin_pipe_write = os.pipe()

    grep_headeronly_cmd = ["grep", "-v", "^[#][#]bcftools"]
    grep_headeronly_p = run_child_cmd(grep_headeronly_cmd,
                                      stdin=grep_headeronly_stdin_pipe_read,
                                      stdout=final_headeronly_tmp_f,
                                      tag="bcftools concat (headeronly)")
    bcftools_concat_headeronly_cmd = ["bcftools", "concat", "-Ov", "-f", concat_headeronly_tmp_fofn]
    bcftools_concat_headeronly_p = run_child_cmd(bcftools_concat_headeronly_cmd,
                                                 stdout=grep_headeronly_stdin_pipe_write,
                                                 tag="bcftools concat (headeronly)")
    while ((bcftools_concat_headeronly_p and bcftools_concat_headeronly_p.poll() is None) 
           or (grep_headeronly_p and grep_headeronly_p.poll() is None)):
        watch_fds_and_print_output()
        bcftools_concat_headeronly_p = close_process_if_finished(bcftools_concat_headeronly_p,
                                                                 "bcftools concat (headeronly)",
                                                                 close_fds=[grep_headeronly_stdin_pipe_write])
        grep_headeronly_p = close_process_if_finished(grep_headeronly_p,
                                                      "bcftools concat (headeronly)",
                                                      close_fds=[grep_headeronly_stdin_pipe_read],
                                                      close_files=[final_headeronly_tmp_f])

    if bcftools_concat_headeronly_p is not None:
        print "ERROR: failed to cleanly terminate bcftools concat (headeronly)"

    if grep_headeronly_p is not None:
        print "ERROR: failed to cleanly terminate grep (headeronly)"

    print "Creating final 'cat | bcftools view -Oz' pipe"
    final_bcftools_view_stdin_pipe_read, final_bcftools_view_stdin_pipe_write = os.pipe()
    print "Preparing penultimate output file [%s]" % (penultimate_out_file)
    final_bcftools_view_cmd = ["bcftools", "view", "-Oz", "-o", penultimate_out_file]
    final_concat_cmd = ["cat", final_headeronly_tmp, out_file_tmp]
    final_bcftools_view_p = run_child_cmd(final_bcftools_view_cmd, tag="final bcftools view -Oz", stdin=final_bcftools_view_stdin_pipe_read)
    final_concat_p = run_child_cmd(final_concat_cmd, tag="final cat (header+data)", stdout=final_bcftools_view_stdin_pipe_write)
    while True:
        watch_fds_and_print_output()
        final_bcftools_view_p = close_process_if_finished(final_bcftools_view_p,
                                                          "final bcftools view -Oz",
                                                          close_fds=[final_bcftools_view_stdin_pipe_read])
        final_concat_p = close_process_if_finished(final_concat_p,
                                                   "final cat (header+data)",
                                                   close_fds=[final_bcftools_view_stdin_pipe_write])
        if not ((final_concat_p and final_concat_p.poll() is None)
                or (final_bcftools_view_p and final_bcftools_view_p.poll() is None)):
            # none of the processes are still running, we're done! 
            break
        else:
            sleep(0.01)
            # continue to next loop iteration

    if final_bcftools_view_p is not None:
        print "ERROR: failed to cleanly terminate final bcftools view -Oz"

    if final_concat_p is not None:
        print "ERROR: failed to cleanly terminate final cat (header+data)"

    print "Reheadering penultimate output file into final out file [%s]" % (final_out_file)
    final_bcftools_reheader_cmd = ["bcftools", "reheader", "-h", final_headeronly_tmp, "-o", final_out_file, penultimate_out_file]
    final_bcftools_reheader_p = run_child_cmd(final_bcftools_reheader_cmd, tag="final bcftools reheader")
    while True:
        watch_fds_and_print_output()
        final_bcftools_reheader_p = close_process_if_finished(final_bcftools_reheader_p,
                                                          "final bcftools reheader")
        if not (final_bcftools_reheader_p and final_bcftools_reheader_p.poll() is None):
            # none of the processes are still running, we're done! 
            break
        else:
            sleep(0.01)
            # continue to next loop iteration
    if final_bcftools_reheader_p is not None:
        print "ERROR: failed to cleanly terminate final bcftools reheader"

    print "Indexing final output file [%s]" % (final_out_file)
    bcftools_index_cmd = ["bcftools", "index", final_out_file]
    bcftools_index_p = run_child_cmd(bcftools_index_cmd, tag="bcftools index")
    while (bcftools_index_p and bcftools_index_p.poll() is None):
        watch_fds_and_print_output()
    bcftools_index_p = close_process_if_finished(bcftools_index_p,
                                               "bcftools index")
    if bcftools_index_p is not None:
        print "ERROR: failed to cleanly terminate bcftools index"

    print "Complete, removing temporary files"
    os.remove(concat_headeronly_tmp_fofn)
    os.remove(final_headeronly_tmp)
    os.remove(penultimate_out_file)
    os.remove(out_file_tmp)
    for tmp_file in tmp_files_to_delete:
        os.remove(tmp_file)

    # Write a new collection as output
    out = arvados.CollectionWriter()

    # Write out_dir to keep
    print "Writing Keep Collection from %s to %s" % (out_dir, stream_name)
    out.write_directory_tree(out_dir, stream_name)

    # Commit the output to Keep.
    output_locator = out.finish()
    print "Task output locator [%s]" % output_locator

    # Use the resulting locator as the output for this task.
    this_task.set_output(output_locator)

    # Done!


if __name__ == '__main__':
    main()

