#!/bin/bash
#SBATCH --job-name=vasp-job
#SBATCH --output=%x-%j.log
#SBATCH --error=%x-%j.err

set -euo pipefail

cd "${SLURM_SUBMIT_DIR}"
ulimit -s unlimited

: "${SLURM_NTASKS:?submit with an explicit --ntasks value}"
mpirun -np "${SLURM_NTASKS}" vasp_std > vasp.log 2>&1
