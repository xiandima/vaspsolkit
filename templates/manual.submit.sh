#!/bin/bash
set -euo pipefail

echo "=========================================="
echo "Host:        $(hostname)"
echo "Workdir:     $(pwd)"
echo "Start:       $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# Configure these environment variables for the local machine.
: "${VASP_TASKS:?set VASP_TASKS to the total MPI rank count}"
if [[ -n "${VASP_MODULE:-}" ]]; then
    module load "${VASP_MODULE}"
fi
mpirun -np "${VASP_TASKS}" "${VASP_EXECUTABLE:-vasp_std}"

echo "=========================================="
echo "End:         $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
