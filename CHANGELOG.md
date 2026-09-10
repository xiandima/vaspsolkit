# Changelog

## 0.4.0 - Unreleased

- Separate the quadratic energy vertex from the charge-zero PZC estimate.
- Add fit-domain and scientific-provenance metadata to postprocessing output.
- Reject reaction inputs with unknown or incompatible provenance by default.
- Reject reaction-curve extrapolation unless explicitly allowed.
- Remove site resource values from the public SLURM script template.
- Document E-U formulas, FERMI_SHIFT limitations, INCAR profiles, validation layers, and cluster configuration.
- Correct CLI migration and repair examples; separate detailed audit output from CSV-only fit reports.
- Clarify that method fingerprints are user declarations, LOCPOT maxima are not validated vacuum plateaus, and external correction conventions are not automatically imported.
- Disclose reaction compatibility overrides, missing domains, source metadata, constants, and formulas in generated reports.
- Preserve known domain checks for legacy inputs and reject malformed/nonfinite fitted domains.
- Keep a single VASP launch when rewriting the public SLURM template; report actual included/excluded E-U point counts.
- Document actual SLURM launcher, memory, custom-adapter, offline installation, and committed-HEAD release boundaries.
