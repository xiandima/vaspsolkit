# Minimal case layout

This directory intentionally contains no real calculation inputs. Before starting a case, create the following files from your own validated VASP setup:

```text
my-case/
├── POSCAR
├── INCAR
├── KPOINTS
├── POTCAR
└── vasp.slurm
```

Never publish `POTCAR`: its redistribution is controlled by the pseudopotential licence. Adapt `vasp.slurm` to the target SLURM cluster and test it with a normal VASP job before using VASPsolKit.
