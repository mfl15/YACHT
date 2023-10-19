import os, sys
import numpy as np
import warnings
from scipy.stats import binom
from scipy.special import betaincinv
import pandas as pd
import zipfile
from tqdm import tqdm, trange
from .utils import load_signature_with_ksize
import concurrent.futures as cf
from multiprocessing import Pool, Manager
import sourmash
from typing import Optional, Union, List, Set, Dict, Tuple
warnings.filterwarnings("ignore")
from loguru import logger
logger.remove()
logger.add(sys.stdout, format="{time:YYYY-MM-DD HH:mm:ss} - {level} - {message}", level="INFO")

def get_organisms_with_nonzero_overlap(manifest: pd.DataFrame, sample_file: str, scale: int, ksize: int, num_threads: int, path_to_genome_temp_dir: str, path_to_sample_temp_dir: str) -> List[str]:
    """
    This function runs the sourmash multisearch to find the organisms that have non-zero overlap with the sample.
    :param manifest: a dataframe with the following columns:
                        'organism_name',
                        'md5sum',
                        'num_unique_kmers_in_genome_sketch',
                        'num_total_kmers_in_genome_sketch',
                        'genome_scale_factor',
                        'num_exclusive_kmers_in_sample_sketch',
                        'num_total_kmers_in_sample_sketch',
                        'sample_scale_factor',
                        'min_coverage'
    :param sample_file: string (path to the sample signature file)
    :param scale: int (scale factor)
    :param ksize: string (size of kmer)
    :param num_threads: int (number of threads to use for parallelization)
    :param path_to_genome_temp_dir: string (path to the genome temporary directory generated by the training step)
    :param path_to_sample_temp_dir: string (path to the sample temporary directory)
    :return: a list of organism names that have non-zero overlap with the sample
    """    
    # run the sourmash multisearch
    # prepare the input files for the sourmash multisearch
    # unzip the sourmash signature file to the temporary directory
    logger.info("Unzipping the sample signature zip file")
    with zipfile.ZipFile(sample_file, 'r') as sample_zip_file:
        sample_zip_file.extractall(path_to_sample_temp_dir)
    
    sample_sig_file = pd.DataFrame([os.path.join(path_to_sample_temp_dir, 'signatures', sig_file) for sig_file in os.listdir(os.path.join(path_to_sample_temp_dir, 'signatures'))])
    sample_sig_file_path = os.path.join(path_to_sample_temp_dir, 'sample_sig_file.txt')
    sample_sig_file.to_csv(sample_sig_file_path, header=False, index=False)
    
    organism_sig_file = pd.DataFrame([os.path.join(path_to_genome_temp_dir, 'signatures', md5sum+'.sig.gz') for md5sum in manifest['md5sum']])
    organism_sig_file_path = os.path.join(path_to_sample_temp_dir, 'organism_sig_file.txt')
    organism_sig_file.to_csv(organism_sig_file_path, header=False, index=False)
    
    # run the sourmash multisearch
    cmd = f"sourmash scripts multisearch {sample_sig_file_path} {organism_sig_file_path} -s {scale} -k {ksize} -c {num_threads} -t 0 -o {os.path.join(path_to_sample_temp_dir, 'sample_multisearch_result.csv')}"
    logger.info(f"Running sourmash multisearch with command: {cmd}")
    exit_code = os.system(cmd)
    if exit_code != 0:
        raise ValueError(f"Error running sourmash multisearch with command: {cmd}")

    # read the multisearch result
    multisearch_result = pd.read_csv(os.path.join(path_to_sample_temp_dir, 'sample_multisearch_result.csv'), sep=',', header=0)
    multisearch_result = multisearch_result.drop_duplicates().reset_index(drop=True)
    
    return multisearch_result['match_name'].to_list()

def get_exclusive_hashes(manifest: pd.DataFrame, nontrivial_organism_names: List[str], sample_sig: sourmash.SourmashSignature, ksize: int, path_to_genome_temp_dir: str) -> Tuple[List[Tuple[int, int]], pd.DataFrame]:
    """
    This function gets the unique hashes exclusive to each of the organisms that have non-zero overlap with the sample, and
    then find how many are in the sampe.
    :param manifest: a dataframe with the following columns:
                        'organism_name',
                        'md5sum',
                        'num_unique_kmers_in_genome_sketch',
                        'num_total_kmers_in_genome_sketch',
                        'genome_scale_factor',
                        'num_exclusive_kmers_in_sample_sketch',
                        'num_total_kmers_in_sample_sketch',
                        'sample_scale_factor',
                        'min_coverage'
    :param nontrivial_organism_names: a list of organism names that have non-zero overlap with the sample
    :param sample_sig: the sample signature
    :param ksize: int (size of kmer)
    :param num_threads: int (number of threads to use for parallelization)
    :param path_to_genome_temp_dir: string (path to the genome temporary directory generated by the training step)
    :return: 
        a list of tuples, each tuple contains the following information:
            1. the number of unique hashes exclusive to the organism under consideration
            2. the number of unique hashes exclusive to the organism under consideration that are in the sample
        a new manifest dataframe that only contains the organisms that have non-zero overlap with the sample
    """
    
    def __find_exclusive_hashes(md5sum, path_to_temp_dir, ksize, single_occurrence_hashes):
        # load genome signature
        sig = load_signature_with_ksize(os.path.join(path_to_temp_dir, 'signatures', md5sum+'.sig.gz'), ksize)
        return {hash for hash in sig.minhash.hashes if hash in single_occurrence_hashes}
    
    # get manifest information for the organisms that have non-zero overlap with the sample
    sub_manifest = manifest.loc[manifest['organism_name'].isin(nontrivial_organism_names),:].reset_index(drop=True)
    organism_md5sum_list = sub_manifest['md5sum'].to_list()

    single_occurrence_hashes = set()
    multiple_occurrence_hashes = set()
    for md5sum in tqdm(organism_md5sum_list):
        sig = load_signature_with_ksize(os.path.join(path_to_genome_temp_dir, 'signatures', md5sum+'.sig.gz'), ksize)
        for hash in sig.minhash.hashes:
            if hash in multiple_occurrence_hashes:
                continue
            elif hash in single_occurrence_hashes:
                single_occurrence_hashes.remove(hash)
                multiple_occurrence_hashes.add(hash)
            else:
                single_occurrence_hashes.add(hash)
    del multiple_occurrence_hashes # free up memory
    
    # Find hashes that are unique to each organism
    logger.info("Finding hashes that are unique to each organism")
    exclusive_hashes_org = []
    for md5sum in tqdm(organism_md5sum_list, desc='Finding exclusive hashes'):
        exclusive_hashes_org.append(__find_exclusive_hashes(md5sum, path_to_genome_temp_dir, ksize, single_occurrence_hashes))
    del single_occurrence_hashes # free up memory

    # Get sample hashes
    sample_hashes = set(sample_sig.minhash.hashes)
    
    # Find hashes that are unique to each organism and in the sample
    logger.info("Finding hashes that are unique to each organism and in the sample")
    exclusive_hashes_info = []
    for i in trange(len(exclusive_hashes_org)):
        exclusive_hashes = exclusive_hashes_org[i]
        exclusive_hashes_info.append((len(exclusive_hashes), len(exclusive_hashes.intersection(sample_hashes))))
    
    return exclusive_hashes_info, sub_manifest


def get_alt_mut_rate(nu: int, thresh: int, ksize: int, significance: float = 0.99) -> float:
    """
    Computes the alternative mutation rate for a given significance level. I.e. how much higher would the mutation rate
    have needed to be in order to have a false positive rate of significance (since we are setting the false negative
    rate to significance by design)?
    :param nu: int (Number of k-mers exclusive to the organism under consideration)
    :param thresh: Number of exclusive k-mers I would need to observe in order to reject the null hypothesis (i.e.
    accept that the organism is present)
    :param ksize: int (k-mer size)
    :param significance: value between 0 and 1 expressing the desired false positive rate (and by design, the false
    negative rate)
    :return: float (alternative mutation rate; how much higher would the mutation rate have needed to be in order to
    make FP and FN rates equal to significance)
    """
    # Replace binary search with the regularized incomplete Gamma function inverse: Solve[significance ==
    #   BetaRegularized[1 - (1 - mutCurr)^k, nu - thresh,
    #    1 + thresh], mutCurr]
    # per mathematica
    mut = 1 - (1 - betaincinv(nu - thresh, 1 + thresh, significance))**(1/ksize)
    return -1.0 if np.isnan(mut) else mut


def single_hyp_test(
    exclusive_hashes_info_org: Tuple[int, int],
    ksize: int,
    significance: float = 0.99,
    ani_thresh: float = 0.95,
    min_coverage: int = 1
) -> Tuple[bool, float, int, int, int, int, float, float]:
    """
    Performs a single hypothesis test for the presence of a genome in a metagenome.
    :param exclusive_hashes_info_org: a tuple containing the following information:
            1. the number of unique hashes exclusive to this genome under consideration
            2. the number of unique hashes exclusive to this genome under consideration that are in the sample
    :param ksize: int (k-mer size)
    :param significance: float (significance level for the hypothesis test)
    :param ani_thresh: threshold for ANI (i.e. how similar do the genomes need to be in order to be considered the same)
    :param min_coverage: minimum coverage of the genome under consideration in the metagenome (float in [0, 1])
    :return: A whole bunch of stuff
    """
    # get the number of unique k-mers
    num_exclusive_kmers = exclusive_hashes_info_org[0]
    # mutation rate
    non_mut_p = (ani_thresh)**ksize
    # # assuming coverage of 1, how many unique k-mers would I need to observe in order to reject the null hypothesis?
    # acceptance_threshold_wo_coverage = binom.ppf(1-significance, num_exclusive_kmers, non_mut_p)
    # # what is the actual confidence of the test?
    # actual_confidence_wo_coverage = 1-binom.cdf(acceptance_threshold_wo_coverage, num_exclusive_kmers, non_mut_p)
    # number of unique k-mers I would see given a coverage of min_coverage
    num_exclusive_kmers_coverage = int(num_exclusive_kmers * min_coverage)
    # how many unique k-mers would I need to observe in order to reject the null hypothesis,
    # assuming coverage of min_cov?
    acceptance_threshold_with_coverage = binom.ppf(1-significance, num_exclusive_kmers_coverage, non_mut_p)
    # what is the actual confidence of the test, assuming coverage of min_cov?
    actual_confidence_with_coverage = 1-binom.cdf(acceptance_threshold_with_coverage, num_exclusive_kmers_coverage,
                                                  non_mut_p)
    # # what is the alternative mutation rate? I.e. how much higher would the mutation rate (resp. how low of ANI)
    # # have needed to be in order to have a false positive rate of significance
    # # (since we are setting the false negative rate to significance by design)?
    # alt_confidence_mut_rate = get_alt_mut_rate(num_exclusive_kmers, acceptance_threshold_wo_coverage, ksize,
    #                                            significance=significance)
    # same as above, but assuming coverage of min_cov
    alt_confidence_mut_rate_with_coverage = get_alt_mut_rate(num_exclusive_kmers_coverage,
                                                             acceptance_threshold_with_coverage,
                                                             ksize, significance=significance)

    # How many unique k-mers do I actually see?
    num_matches = exclusive_hashes_info_org[1]
    p_val = binom.cdf(num_matches, num_exclusive_kmers, non_mut_p)
    # is the genome present? Takes coverage into account
    in_sample_est = (num_matches >= acceptance_threshold_with_coverage) and (num_matches != 0) and (acceptance_threshold_with_coverage != 0)
    # return in_sample_est, p_val, num_exclusive_kmers, num_exclusive_kmers_coverage, num_matches, \
    #        acceptance_threshold_wo_coverage, acceptance_threshold_with_coverage, actual_confidence_wo_coverage, \
    #        actual_confidence_with_coverage, alt_confidence_mut_rate, alt_confidence_mut_rate_with_coverage
    return in_sample_est, p_val, num_exclusive_kmers, num_exclusive_kmers_coverage, num_matches, \
           acceptance_threshold_with_coverage, actual_confidence_with_coverage, alt_confidence_mut_rate_with_coverage

def hypothesis_recovery(
    manifest: pd.DataFrame,
    sample_info_set: Tuple[str, sourmash.SourmashSignature],
    path_to_genome_temp_dir: str,
    min_coverage_list: List[float],
    scale: int,
    ksize: int,
    significance: float = 0.99,
    ani_thresh: float = 0.95,
    num_threads: int = 16
):
    """
    Go through each of the organisms that have non-zero overlap with the sample and perform a hypothesis test for the
    presence of that organism in the sample: have we seen enough k-mers exclusive to that organism to conclude that
    an organism with ANI > ani_thresh (to the one under consideration) is present in the sample?
    :param manifest: a dataframe with the following columns: 
                        'organism_name', 
                        'md5sum', 
                        'num_unique_kmers_in_genome_sketch',
                        'num_total_kmers_in_genome_sketch', 
                        'genome_scale_factor',
                        'num_exclusive_kmers_in_sample_sketch',
                        'num_total_kmers_in_sample_sketch', 
                        'sample_scale_factor',
                        'min_coverage'
    :param sample_info_set: a set of information about the sample, including the sample signature location and the sample signature object
    :param path_to_genome_temp_dir: path to the genome temporary directory generated by the training step
    :param min_coverage_list: a list of minimum coverage values
    :param scale: scale factor
    :param ksize: k-mer size
    :param significance: significance level for the hypothesis test
    :param ani_thresh: threshold for ANI (i.e. how similar do the genomes need to be in order to be considered the same)
    :param num_threads: number of threads to use for parallelization
    :return: a list of pandas dataframe with the results of the hypothesis tests based on different min_coverage values
    """
    
    # unpack the sample info set
    sample_file, sample_sig = sample_info_set
    
    # create a temporary directory for the sample
    sample_dir = os.path.dirname(sample_file)
    sample_name = os.path.basename(sample_file).replace('.sig.zip', '')
    path_to_sample_temp_dir = os.path.join(sample_dir, f'sample_{sample_name}_intermediate_files')
    if not os.path.exists(path_to_sample_temp_dir):
        os.makedirs(path_to_sample_temp_dir)
    
    # Find the organisms that have non-zero overlap with the sample
    nontrivial_organism_names = get_organisms_with_nonzero_overlap(manifest, sample_file, scale, ksize, num_threads, path_to_genome_temp_dir, path_to_sample_temp_dir)
    
    # Get the unique hashes exclusive to each of the organisms that have non-zero overlap with the sample
    exclusive_hashes_info, manifest = get_exclusive_hashes(manifest, nontrivial_organism_names, sample_sig, ksize, path_to_genome_temp_dir)
    
    # Set up the results dataframe columns
    given_columns = [
                'in_sample_est',  # Main output: Boolean indicating whether genome is present in sample
                'p_vals',  # Probability of observing this or more extreme result at ANI threshold.
                'num_exclusive_kmers_to_genome',  # Number of k-mers exclusive to genome
                'num_exclusive_kmers_to_genome_coverage',  # Number of k-mers exclusive to genome, assuming coverage of min_cov
                'num_matches',  # Number of k-mers exclusive to genome that are present in the sample
                # 'acceptance_threshold_wo_coverage',  # Acceptance threshold without adjusting for coverage
                # (how many k-mers need to be present in order to reject the null hypothesis)
                'acceptance_threshold_with_coverage',  # Acceptance threshold with adjusting for coverage
                # 'actual_confidence_wo_coverage',  # Actual confidence without adjusting for coverage
                'actual_confidence_with_coverage',  # Actual confidence with adjusting for coverage
                # 'alt_confidence_mut_rate',  # Mutation rate such that at this mutation rate, false positive rate = p_val.
                # Does not account for min_coverage parameter.
                'alt_confidence_mut_rate_with_coverage',  # same as above, but accounting for min_coverage parameter
            ]
    
    # Using multiprocessing.Pool to parallelize the execution
    manifest_list = []
    for min_coverage in tqdm(min_coverage_list, desc='Computing hypothesis recovery'):
        logger.info(f"Computing hypothesis recovery for min_coverage={min_coverage}")
        with Pool(processes=num_threads) as p:
            params = ((exclusive_hashes_info[i], ksize, significance, ani_thresh, min_coverage) for i in range(len(exclusive_hashes_info)))
            results = p.starmap(single_hyp_test, params)

        # Create a pandas dataframe to store the results
        results = pd.DataFrame(results, columns=given_columns)
    
        # combine the results with the manifest
        manifest['min_coverage'] = min_coverage
        manifest_list.append(pd.concat([manifest, results], axis=1))
    
    return manifest_list