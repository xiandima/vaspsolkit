#!/bin/bash
#SBATCH --job-name=vasp-job
#SBATCH --partition=<partition>
#SBATCH --nodes=1
#SBATCH --ntasks=<total-mpi-ranks>
#SBATCH --time=<walltime>
#SBATCH --output=%x-%j.log

set -euo pipefail

echo "=========================================="
echo "Job ID:      ${SLURM_JOB_ID:-unknown}"
echo "Job name:    ${SLURM_JOB_NAME:-unknown}"
echo "Node list:   ${SLURM_NODELIST:-unknown}"
echo "Tasks:       ${SLURM_NTASKS:-unknown}"
echo "Workdir:     $(pwd)"
echo "Start:       $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

module purge
module load <vasp-module>

srun <vasp-executable>

echo "=========================================="
echo "End:         $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
