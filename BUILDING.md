# Building Chromix for Windows x64

Chromix currently packages Windows x64 only. It builds on the pinned `ungoogled-chromium` source and Windows overlay,
then applies the Chromium 152 patch series in `patches/series`.

## Pinned source layers

| Layer | Version | Commit |
|---|---|---|
| Chromium | `152.0.7977.75` | source archive selected by ungoogled-chromium |
| ungoogled-chromium | `152.0.7977.75-1` | `cacf0f0fd2446a837528c54df1880b75874b9580` |
| ungoogled-chromium-windows | `152.0.7977.75-1.1` | `c8b4eadc799fb40fb0d7acd30b542f130e1f0a17` |
| Chromix patches | `patches/series` | content hash stored in source markers |

The machine-readable pins are in `CHROMIUM_VERSION`, `UNGOOGLED_VERSION`,
`UNGOOGLED_WINDOWS_VERSION`, and `build/ungoogled-revisions.psd1`.

## Native Windows build

Prerequisites:

- Visual Studio 2022 with the Desktop development with C++ workload
- Windows 11 SDK 10.0.26100 and its Debugging Tools feature
- Python 3, Git, PowerShell 7, and 7-Zip
- about 120 GB free disk space

Run from the repository root in a Developer PowerShell:

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Jobs 8
```

The script performs these layers in order:

1. checks out the pinned ungoogled-chromium repositories;
2. downloads and unpacks the pinned Chromium source archive;
3. downloads the Windows toolchain dependencies from the pinned Windows overlay;
4. prunes binaries using the ungoogled pruning list and assembles the downloaded
   x64 Rust components into Chromium's `third_party/rust-toolchain` layout;
5. applies ungoogled-chromium core patches;
6. applies the ungoogled-chromium-windows overlay;
7. applies every Chromix patch in `patches/series`;
8. merges ungoogled common GN flags, Windows flags, and `build/args.windows.gn`;
9. bootstraps GN and bindgen, then builds `chrome` with Ninja.

The output browser is:

```text
D:\chromix-build\src\out\Chromix\chrome.exe
```

Resume an interrupted compile with the same work directory:

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Resume -Jobs 8
```

For GitHub Actions, stage 1 can optionally import a live `build-artifact` from a
matching public `ungoogled-chromium-windows` x64 run. Set the workflow input
`upstream_run_id` and repository secret `UPSTREAM_ACTIONS_TOKEN` (a token with
Actions read access). The imported source and object tree is reused where GN and
Ninja inputs remain compatible; Chromix patches and differing GN arguments
invalidate affected outputs automatically.

`-Resume` still validates the prepared source marker against the current
ungoogled pins and patch-content hash. A stale or mixed source tree is rejected.

## Optional domain substitution

Ungoogled domain substitution is deferred until all build-time downloads have
completed because applying it earlier rewrites toolchain download URLs.
To apply it before `gn gen`, use:

```powershell
pwsh build/windows/build.ps1 -WorkDir D:\chromix-build -Resume -ApplyDomainSubstitution
```

The default build already uses ungoogled source patches and GN flags to disable
Google integrations and reporting; the optional substitution additionally
rewrites domains listed by the Windows overlay.

## Windows x64 package

```powershell
pwsh build/windows/package-win.ps1 `
  -Out D:\chromix-build\src\out\Chromix `
  -Dest D:\chromix-build\dist
```

This creates `chromix-win-x64.zip` and `SHA256SUMS`. The archive contains
`chromix/chromix.cmd`, which is the launcher used by the Python and Node SDKs.

## Patch maintenance

Chromix patches are Chromium 152 ports of the Clearcote-derived engine changes.
Clearcote is the behavioral reference, but its Chromium 149 patch files are
rebased and split into one target file per patch before entering `patches/series`;
applying the original directory on top would duplicate writers and target the
wrong source revision.

The Clearcote Runtime suppression, remote Canvas/WebGL Bridge, and fake WebRTC
srflx ports are compiled in but default-off. See `patches/README.md` for their
explicit `--uxr-*` switches and operational risks.

Check repository patch invariants with:

```powershell
python tools/check_patches.py
```

A full patch-application check requires a clean prepared source tree and is
performed automatically by `prepare-ungoogled.ps1`.
