#!/bin/bash
set -euo pipefail

echo "=========================================="
echo "Host:        $(hostname)"
echo "Workdir:     $(pwd)"
echo "Start:       $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# Replace these placeholders for the local machine.
module load <vasp-module>
mpirun -np <total-mpi-ranks> <vasp-executable>

echo "=========================================="
echo "End:         $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
