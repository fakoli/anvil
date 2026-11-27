# Anvil State: names and compatibility

**Anvil State** is the local-first project-state engine at
[anvilstate.com](https://anvilstate.com/). It is not Anvil Serving, the separate
inference-serving project. Use “Anvil State” in headings, application labels and
prose. Keep commands, paths, package IDs and recorded output literal.

| Surface | Name to use | Compatibility contract |
| --- | --- | --- |
| Install from PyPI | `uv tool install anvil-state` | Same distribution; no uninstall, new package claim or data migration. |
| Run the CLI | `anvil status --json` | This works on existing releases. Source builds containing the rename also install `anvil-state` as a second spelling of the same entry point; older wheels do not. Keep using `anvil` in scripts that must support them. Human-readable help text now uses Anvil State. |
| Start Web or MCP | `anvil-web`, `anvil-mcp` | Existing launchers, registered MCP server `anvil`, tool names and protocol version are unchanged. |
| Import or configure | Python `anvil`, environment `ANVIL_*` | A distribution name is not an import path. The Python API, versioned JSON CLI contracts, HTTP headers and schema IDs are unchanged. |
| State on disk | `.anvil/`, `~/.anvil/workspaces/` | No rename or copy. Do not make an empty replacement state. See [state resolution](../how-to/state-location.md). |
| Install harness adapters | Claude `anvil@anvil`, Codex plugin `anvil`, Pi `anvil-pi`, OpenClaw `anvil-finish-gate` | Display labels can change, but IDs stay stable to avoid duplicate tools or disconnected configurations. |
| Clone source | [fakoli/anvil](https://github.com/fakoli/anvil) | This is the current GitHub repository. Do not substitute a guessed new URL. |

## Maintainer checklist for a future GitHub slug change

A repository rename is a separate operational change, not a requirement to ship
the product name. Coordinate the following in a single maintenance window:

1. Confirm control of the destination name. Inventory clone and release URLs,
   badges, Pages paths, marketplace sources, Docker catalog metadata, pinned
   fleet installs, CI checkout references, docs build source allowlists and
   downstream consumers. Record a known-good wheel and docs revision. Keep
   old attestations and published artifacts as historical records.
2. Verify the existing PyPI project’s GitHub Trusted Publisher: repository owner
   and slug, workflow `publish.yml` and environment `pypi`. The release
   workflow also protects jobs with `github.repository == 'fakoli/anvil'`:
   update **all** those guards during cutover, not just the publisher setting.
   Otherwise a renamed repository silently skips its own release jobs. Arrange the
   new binding with the PyPI owner before any new release; verify the release
   job still uses the intended protected environment. Do not publish a test
   release to the production project just to check a rename.
3. Quiesce tag releases and site deploys, rename the repository, update
   checkout remotes **in place** and update the audited URLs. Do not reuse the
   old slug: doing so invalidates GitHub redirects. GitHub does not redirect a
   project's old Pages path or workflow calls to an action hosted by a renamed
   repository. Audit `uses:` references separately; changing a clone URL does
   not fix those. Keep the custom domain and test bookmarked deep links and assets.
4. Do **not** rename local checkout directories at the same time. The default
   workspace key includes the absolute canonical checkout path. From the same
   checkout, compare `anvil status --json` and an existing project's state
   path, task IDs and history before/after changing the remote. If a physical
   move is necessary, back up and rehearse a separate migration; never run
   `init --force` as a shortcut.
5. Qualify a built wheel and an sdist-derived wheel in clean environments:
   exercise both CLI names, MCP initialization and existing harness installs;
   verify source links, CI, site navigation and canonical URLs. On the next
   intentional release verify PyPI provenance points at the renamed repository
   and remove the obsolete publisher binding. If a check fails, hold the
   release and serve the last known-good site/package while repairing the binding.

See [GitHub's rename behavior](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository)
and [PyPI's Trusted Publisher configuration](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).
