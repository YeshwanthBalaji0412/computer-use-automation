"""Composition.

The wiring layer: the only place that constructs concrete adapters - Playwright, the
Anthropic client - and hands them to engines that know nothing about either. Everything
in `cua.discovery`, `cua.replay`, `cua.schema` and `cua.policy` depends on interfaces
only, which is what makes "swap the surface" and "replay never touches the LLM"
structural claims rather than conventions.

Keeping these modules in their own package is not cosmetic: the import-linter contracts
in setup.cfg forbid the engine packages from importing Playwright, so a wiring module
living inside `cua.discovery` breaks the build. That is the contract working, and it is
why this package exists.
"""
