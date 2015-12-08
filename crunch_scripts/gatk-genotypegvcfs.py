#!/usr/bin/env python

import os           # Import the os module for basic path manipulation
import arvados      # Import the Arvados sdk module
import re
import subprocess

# TODO: make group_by_regex and max_gvcfs_to_combine parameters
group_by_regex = '[.](?P<group_by>[0-9]+_of_[0-9]+)[.]'

class InvalidArgumentError(Exception):
    pass

class FileAccessError(Exception):
    pass

class APIError(Exception):
    pass

def prepare_gatk_reference_collection(reference_coll):
    """
    Checks that the supplied reference_collection has the required 
    files and only the required files for GATK. 
    Returns: a portable data hash for the reference collection
    """
    # Ensure we have a .fa reference file with corresponding .fai index and .dict
    # see: http://gatkforums.broadinstitute.org/discussion/1601/how-can-i-prepare-a-fasta-file-to-use-as-reference
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
    # Create and return a portable data hash for the ref_input manifest
    try:
        r = arvados.api().collections().create(body={"manifest_text": ref_input}).execute()
        ref_input_pdh = r["portable_data_hash"]
    except:
        raise 
    return ref_input_pdh

def process_stream(stream_name, gvcf_by_group, gvcf_indices, interval_list_by_group, if_sequence, ref_input_pdh):
    print "Finalising stream %s" % stream_name


def one_task_per_group(group_by_regex, ref_input_pdh, 
                       if_sequence=0, and_end_task=True):
    """
    Queue one task for each group of gVCFs and corresponding interval_list
    in the inputs_collection, with grouping based on the value of the named 
    capture group "group_by" in the group_by_regex against the filename in 
    the inputs_collection.

    Each new task will have an "inputs" parameter: a manifest
    containing a set of one or more gVCF files and its corresponding 
    index. 

    Each new task will also have a "ref" parameter: a manifest 
    containing the reference files to use. 

    Note that all gVCFs not matching the group_by_regex are ignored. 

    if_sequence and and_end_task arguments have the same significance
    as in arvados.job_setup.one_task_per_input_file().
    """
    if if_sequence != arvados.current_task()['sequence']:
        return

    group_by_r = re.compile(group_by_regex)

    # prepare interval_lists
    il_coll = arvados.current_job()['script_parameters']['interval_lists_collection']
    il_cr = arvados.CollectionReader(il_coll)
    il_ignored_files = []
    interval_list_by_group = {}
    for s in il_cr.all_streams():
        for f in s.all_files():
            m = re.search(group_by_r, f.name())
            if m:
                group_name = m.group('group_by')
                interval_list_m = re.search(r'\.interval_list', f.name())
                if interval_list_m:
                    if group_name not in interval_list_by_group:
                        interval_list_by_group[group_name] = dict()
                    interval_list_by_group[group_name][s.name(), f.name()] = f
                    continue
            # if we make it this far, we have files that we are ignoring
            il_ignored_files.append("%s/%s" % (s.name(), f.name()))

    # prepare gVCF input collections
    job_input = arvados.current_job()['script_parameters']['inputs_collection']
    cr = arvados.CollectionReader(job_input)
    ignored_files = []
    last_stream_name = ""
    gvcf_by_group = {}
    gvcf_indices = {}
    for s in sorted(cr.all_streams(), key=lambda stream: stream.name()):
        for f in s.all_files():
            if re.search(r'\.tbi$', f.name()):
                gvcf_indices[s.name(), f.name()] = f
                continue
            m = re.search(group_by_r, f.name())
            if m:
                group_name = m.group('group_by')
                gvcf_m = re.search(r'\.g\.vcf\.gz$', f.name())
                if gvcf_m:
                    if group_name not in gvcf_by_group:
                        gvcf_by_group[group_name] = dict()
                    gvcf_by_group[group_name][s.name(), f.name()] = f
                    continue
                interval_list_m = re.search(r'\.interval_list', f.name())
                if interval_list_m:
                    if group_name not in interval_list_by_group:
                        interval_list_by_group[group_name] = dict()
                    if (s.name(), f.name()) in interval_list_by_group[group_name]:
                        if interval_list_by_group[group_name][s.name(), f.name()].as_manifest() != f.as_manifest():
                            raise InvalidArgumentError("Already have interval_list for group %s file %s/%s, but manifests are not identical!" % (group_name, s.name(), f.name()))
                    else: 
                        interval_list_by_group[group_name][s.name(), f.name()] = f
                    continue
            # if we make it this far, we have files that we are ignoring
            ignored_files.append("%s/%s" % (s.name(), f.name()))

    # process each group
    for group_name in sorted(gvcf_by_group.keys()):
        print "Have %s gVCFs in group %s" % (len(gvcf_by_group[group_name]), group_name)
        # require interval_list for this group
        if group_name not in interval_list_by_group:
            raise InvalidArgumentError("Inputs collection did not contain interval_list for group %s" % group_name)
        interval_lists = interval_list_by_group[group_name].keys()
        if len(interval_lists) > 1:
            raise InvalidArgumentError("Inputs collection contained more than one interval_list for group %s: %s" % (group_name, ' '.join(interval_lists)))
        task_inputs_manifest = interval_list_by_group[group_name].get(interval_lists[0]).as_manifest()
        for ((s_name, gvcf_name), gvcf_f) in gvcf_by_group[group_name].items():
            task_inputs_manifest += gvcf_f.as_manifest()
            gvcf_index_f = gvcf_indices.get((s_name, re.sub(r'g.vcf.gz$', 'g.vcf.tbi', gvcf_name)), 
                                            gvcf_indices.get((s_name, re.sub(r'g.vcf.gz$', 'g.vcf.gz.tbi', gvcf_name)), 
                                                             None))
            if gvcf_index_f:
                task_inputs_manifest += gvcf_index_f.as_manifest()
            else:
                # no index for gVCF - TODO: should this be an error or warning?
                print "WARNING: No correponding .tbi index file found for gVCF file %s" % gvcf_name
                #raise InvalidArgumentError("No correponding .tbi index file found for gVCF file %s" % gvcf_name)

        # Create a portable data hash for the task's subcollection
        try:
            r = arvados.api().collections().create(body={"manifest_text": task_inputs_manifest}).execute()
            task_inputs_pdh = r["portable_data_hash"]
        except:
            raise 

        # Create task to process this group
        print "Creating new task to process %s" % group_name
        new_task_attrs = {
                'job_uuid': arvados.current_job()['uuid'],
                'created_by_job_task_uuid': arvados.current_task()['uuid'],
                'sequence': if_sequence + 1,
                'parameters': {
                    'inputs': task_inputs_pdh,
                    'ref': ref_input_pdh,
                    'name': group_name
                    }
                }
        arvados.api().job_tasks().create(body=new_task_attrs).execute()

    # report on any ignored files
    if len(ignored_files) > 0:
        print "WARNING: ignored non-matching files in inputs_collection: %s" % (' '.join(ignored_files))
        # TODO: could use `setmedian` from https://github.com/ztane/python-Levenshtein
        # to print most representative "median" filename (i.e. skipped 15 files like median), then compare the 
        # rest of the files to that median (perhaps with `ratio`) 

    if and_end_task:
        print "Ending task %s successfully" % if_sequence
        arvados.api().job_tasks().update(uuid=arvados.current_task()['uuid'],
                                         body={'success':True}
                                         ).execute()
        exit(0)

def mount_gatk_reference(ref_param="ref"):
    # Get reference FASTA
    print "Mounting reference FASTA collection"
    ref_dir = arvados.get_task_param_mount(ref_param)

    # Sanity check reference FASTA
    for f in arvados.util.listdir_recursive(ref_dir):
        if re.search(r'\.fa$', f):
            ref_file = os.path.join(ref_dir, f)
    if ref_file is None:
        raise InvalidArgumentError("No reference fasta found in reference collection.")
    # Ensure we can read the reference file
    if not os.access(ref_file, os.R_OK):
        raise FileAccessError("reference FASTA file not readable: %s" % ref_file)
    # TODO: could check readability of .fai and .dict as well?
    return ref_file

def mount_gatk_gvcf_inputs(inputs_param="inputs"):
    # Get input gVCFs for this task
    print "Mounting task input collection"
    inputs_dir = arvados.get_task_param_mount('inputs')

    # Sanity check input gVCFs    
    input_gvcf_files = []
    for f in arvados.util.listdir_recursive(inputs_dir):
        if re.search(r'\.g\.vcf\.gz$', f):
            input_gvcf_files.append(os.path.join(inputs_dir, f))
        elif re.search(r'\.tbi$', f):
            pass
        elif re.search(r'\.interval_list$', f):
            pass
        else:
            print "WARNING: collection contains unexpected file %s" % f
    if len(input_gvcf_files) == 0:
        raise InvalidArgumentError("Expected one or more .g.vcf.gz files in collection (found 0 while recursively searching %s)" % inputs_dir)

    # Ensure we can read the gVCF files and that they each have an index
    for gvcf_file in input_gvcf_files:
        if not os.access(gvcf_file, os.R_OK):
            raise FileAccessError("gVCF file not readable: %s" % gvcf_file)

        # Ensure we have corresponding .tbi index and can read it as well
        (gvcf_file_base, gvcf_file_ext) = os.path.splitext(gvcf_file)
        assert(gvcf_file_ext == ".gz")
        tbi_file = gvcf_file_base + ".gz.tbi"
        if not os.access(tbi_file, os.R_OK):
            tbi_file = gvcf_file_base + ".tbi"
            if not os.access(tbi_file, os.R_OK):
                raise FileAccessError("No readable gVCF index file for gVCF file: %s" % gvcf_file)
    return input_gvcf_files

def mount_gatk_interval_list_input(inputs_param="inputs"):
    # Get interval_list for this task
    print "Mounting task input collection to get interval_list"
    inputs_dir = arvados.get_task_param_mount('inputs')

    # Sanity check input interval_list (there can be only one)
    input_interval_lists = []
    for f in arvados.util.listdir_recursive(inputs_dir):
        if re.search(r'\.interval_list$', f):
            input_interval_lists.append(os.path.join(inputs_dir, f))
    if len(input_interval_lists) != 1:
        raise InvalidArgumentError("Expected exactly one interval_list in inputs collection (found %s)" % len(input_interval_lists))

    assert(len(input_interval_lists) == 1)
    interval_list_file = input_interval_lists[0]

    if not os.access(interval_list_file, os.R_OK):
        raise FileAccessError("interval_list file not readable: %s" % interval_list_file)

    return interval_list_file

def prepare_out_dir():
    # Will write to out_dir, make sure it is empty
    out_dir = os.path.join(arvados.current_task().tmpdir, 'out')
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
    return out_dir

def gatk_genotype_gvcfs(ref_file, interval_list_file, gvcf_files, out_path, extra_args=[]):
    print "gatk_combine_gvcfs called with ref_file=[%s] interval_list_file=[%s] gvcf_files=[%s] out_path=[%s]" % (ref_file, interval_list_file, ' '.join(gvcf_files), out_path)
    # Call GATK GenotypeGVCFs
    gatk_args = [
            "java", "-d64", "-Xmx8g", "-jar", "/gatk/GenomeAnalysisTK.jar", 
            "-T", "GenotypeGVCFs", 
            "-R", ref_file,
            "-L", interval_list_file,
            "-nt", "2"]
    for gvcf_file in gvcf_files:
        gatk_args.extend(["--variant", gvcf_file])
    gatk_args.extend([
        "-o", out_path
    ])
    print "Calling GATK: %s" % gatk_args
    gatk_p = subprocess.Popen(
        gatk_args,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        close_fds=True,
        shell=False)

    gatk_line_num = 0
    while gatk_p.poll() is None:
        line = gatk_p.stdout.readline()
        gatk_line_num += 1
        if gatk_line_num <= 300:
            print "GATK: %s" % line.rstrip()
        elif re.search(r'(FATAL|ERROR|ProgressMeter)', line):
            print "GATK: %s" % line.rstrip()

    gatk_exit = gatk_p.wait()
    return gatk_exit

def main():
    ################################################################################
    # Phase I: Check inputs and setup sub tasks 1-N to process group(s) based on 
    #          applying the capturing group named "group_by" in group_by_regex.
    #          (and terminate if this is task 0) 
    ################################################################################
    ref_input_pdh = prepare_gatk_reference_collection(reference_coll=arvados.current_job()['script_parameters']['reference_collection'])
    one_task_per_group(group_by_regex, 
                       ref_input_pdh, 
                       if_sequence=0, and_end_task=True)

    # We will never reach this point if we are in the 0th task sequence
    assert(arvados.current_task()['sequence'] > 0)

    ################################################################################
    # Phase II: Genotype gVCFs!
    ################################################################################
    ref_file = mount_gatk_reference(ref_param="ref")
    interval_list_file = mount_gatk_interval_list_input(inputs_param="inputs")
    gvcf_files = mount_gatk_gvcf_inputs(inputs_param="inputs")
    out_dir = prepare_out_dir()
    name = arvados.current_task()['parameters'].get('name')
    if not name:
        name = "unknown"
    out_file = name + ".vcf.gz"

    # GenotypeGVCFs! 
    gatk_exit = gatk_genotype_gvcfs(ref_file, interval_list_file, gvcf_files, os.path.join(out_dir, out_file))

    if gatk_exit != 0:
        print "WARNING: GATK exited with exit code %s (NOT WRITING OUTPUT)" % gatk_exit
        arvados.api().job_tasks().update(uuid=arvados.current_task()['uuid'],
                                         body={'success':False}
                                         ).execute()
    else: 
        print "GATK exited successfully, writing output to keep"

        # Write a new collection as output
        out = arvados.CollectionWriter()

        # Write out_dir to keep
        out.write_directory_tree(out_dir)

        # Commit the output to Keep.
        output_locator = out.finish()

        # Use the resulting locator as the output for this task.
        arvados.current_task().set_output(output_locator)

    # Done!


if __name__ == '__main__':
    main()
