# legacy/ — original prototype (read-only reference)

The first prototype of the AI hedge fund, kept unchanged for reference. **Do not run it against a real
account and do not port code from it** — see `docs/08_PROTOTYPE_AUDIT.md` for the defects that led to the
rebuild. Port intent only.

`requirements.txt` here is not installable as pinned (`pandas-ta==0.3.14b0` is no longer on PyPI, and
`MetaTrader5==5.0.45` has no wheels for Python ≥ 3.12).
