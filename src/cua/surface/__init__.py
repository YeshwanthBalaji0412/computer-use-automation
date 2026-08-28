"""Surface adapters: the seam between "how we perceive/act on a surface" and "the recorded flow".

Deliberately empty of re-exports. `cua.replay` and `cua.discovery` import `cua.surface.base`
(pure Pydantic models + an ABC) and receive a concrete `Surface` by dependency injection from
the composition root in `cua.cli`. Re-exporting `WebSurface` here would drag Playwright into
every importer and break the import-linter contract in setup.cfg that confines Playwright to
this package's adapter modules.
"""
