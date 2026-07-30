from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class PbsSpec:
    job_name: str
    workdir: Path
    node: Optional[str] = None
    queue: Optional[str] = None
    ppn: int = 48
    walltime: str = "48:00:00"
    module: Optional[str] = None
    executable: str = "vasp_std"


def render_pbs_script(spec: PbsSpec) -> str:
    workdir = Path(spec.workdir).resolve()
    log_path = workdir / f"{spec.job_name}.log"
    node_request = f"nodes={spec.node}:ppn={spec.ppn}" if spec.node else f"nodes=1:ppn={spec.ppn}"
    queue_line = f"#PBS -q {spec.queue}\n" if spec.queue else ""
    module_lines = f"module load {spec.module}\n" if spec.module else ""
    return (
        "#!/bin/bash\n"
        f"{queue_line}"
        f"#PBS -N {spec.job_name}\n"
        f"#PBS -l {node_request}\n"
        f"#PBS -l walltime={spec.walltime}\n"
        "#PBS -j oe\n"
        f"#PBS -o {log_path}\n"
        f"cd {workdir}\n"
        'echo "=========================================="\n'
        'echo "Job ID:      ${PBS_JOBID}"\n'
        'echo "Job name:    ${PBS_JOBNAME}"\n'
        'echo "Queue:       ${PBS_QUEUE}"\n'
        'echo "Nodes:       $(cat ${PBS_NODEFILE} | sort -u)"\n'
        'echo "Cores:       $(cat ${PBS_NODEFILE} | wc -l)"\n'
        'echo "Workdir:     $(pwd)"\n'
        'echo "Start:       $(date \'+%Y-%m-%d %H:%M:%S\')"\n'
        'echo "=========================================="\n'
        f"{module_lines}"
        f"mpirun -np {spec.ppn} {spec.executable}\n"
        'echo "=========================================="\n'
        'echo "End:         $(date \'+%Y-%m-%d %H:%M:%S\')"\n'
        'echo "=========================================="\n'
    )


def validate_pbs_script(text: str, spec: PbsSpec) -> List[str]:
    workdir = Path(spec.workdir).resolve()
    log_path = workdir / f"{spec.job_name}.log"
    node_request = f"nodes={spec.node}:ppn={spec.ppn}" if spec.node else f"nodes=1:ppn={spec.ppn}"
    required = [
        f"#PBS -N {spec.job_name}",
        f"#PBS -l {node_request}",
        f"#PBS -l walltime={spec.walltime}",
        "#PBS -j oe",
        f"#PBS -o {log_path}",
        f"cd {workdir}",
        f"mpirun -np {spec.ppn} {spec.executable}",
    ]
    if spec.queue:
        required.append(f"#PBS -q {spec.queue}")
    if spec.module:
        required.append(f"module load {spec.module}")
    return [item for item in required if item not in text]


def write_pbs_script(path: Path, spec: PbsSpec) -> None:
    Path(path).write_text(render_pbs_script(spec), encoding="utf-8")
