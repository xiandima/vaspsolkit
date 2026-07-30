# Minimal case layout

This directory intentionally contains no real calculation inputs. Before starting a case, create the following files from your own validated VASP setup:

```text
my-case/
├── POSCAR
├── INCAR
├── KPOINTS
├── POTCAR
└── vasp.pbs
```

Never publish `POTCAR`: its redistribution is controlled by the pseudopotential licence. The batch script must be adapted to the target HPC environment and tested with a normal VASP job before it is used with VASPsolKit.
