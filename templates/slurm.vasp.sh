#!/bin/bash
#SBATCH --job-name=vasp-job
#SBATCH --partition=compute
#SBATCH --nodes=1
#SBATCH --ntasks=96
#SBATCH --ntasks-per-node=96
#SBATCH --time=72:00:00
#SBATCH --output=%x-%j.log

set -euo pipefail

cd "${SLURM_SUBMIT_DIR}"
ulimit -s unlimited

mpirun -np "${SLURM_NTASKS}" vasp_std > vasp.log 2>&1
