#!/usr/bin/env bash
set -e
set -u
set -o pipefail
# This first command need only be run once
echo "Making database"
#python ../../KEGG_sketching_annotation/scripts/create_genome_ref_db.py ./ref_genomes_3/reference_genomes 1046_database 1046

simsFolder=sims-$(date +"%s")
mkdir ${simsFolder}
echo "Creating simulation"
ln -s formatted_db.fasta ${simsFolder}/formatted_db.fasta
python run_sim.py --genomes_folder ref_genomes_3/reference_genomes --out_folder ${simsFolder} --num_reads 10000 --num_orgs 1000


# Note: I could do a loop here if I felt like it
N=500
# remove N genomes from the training datbase
echo "Making ${N} genomes unkown"
cat ${simsFolder}/simulation_counts.csv | shuf | head -n ${N} | cut -d',' -f1 > ${simsFolder}/unknown_names.txt
# remove those from the training datbase
echo "Removing them from the ref db"
../../KEGG_sketching_annotation/utils/bbmap/./filterbyname.sh in=${simsFolder}/formatted_db.fasta out=${simsFolder}/without_unknown_db.fasta names=${simsFolder}/unknown_names.txt include=f overwrite=true
# Sketching the reference 
echo "Sketching the reference"
sourmash sketch dna -f -p k=31,scaled=1000,abund -o ${simsFolder}/without_unknown_db.sig --singleton ${simsFolder}/without_unknown_db.fasta
echo "Making the EU dictionary"
python ../ref_matrix.py --ref_file ${simsFolder}/without_unknown_db.sig  --ksize 31 --out_prefix default_EU_
# then run the methods
echo "sketching the simulation"
sourmash sketch dna -f -p k=31,scaled=1000,abund -o ${simsFolder}/simulated_mg.fq.sig ${simsFolder}/simulated_mg.fq
# Run gather
echo "running gather"
sourmash gather --dna --threshold-bp 100 ${simsFolder}/simulated_mg.fq.sig ${simsFolder}/without_unknown_db.sig -o ${simsFolder}/gather_results.csv

# then run our approach
python ../recover_abundance.py --ref_file ${simsFolder}/default_EU_ref_matrix_processed.npz  --ksize 31 --sample_file ${simsFolder}/simulated_mg.fq.sig --outfile ${simsFolder}/EU_results_default.csv

