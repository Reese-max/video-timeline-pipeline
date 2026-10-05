# C2PA local MP4 validation research — 2026-10-05

This is a bounded research receipt for issue #20. The result supports **NARROW: local original-file inspection only**. It does not enable a product admission gate, establish social-URL support, or satisfy the research issue's production signer, source attribution, and user-friction acceptance.

The previous UUID-shaped metadata experiment was not cryptographic validation. This study uses the real public `c2patool 0.28.1` executable and an existing signed SDK MP4. No media or signing keys are redistributed. Product code and configuration are unchanged.

## Fixed inputs

The [machine receipt](c2pa-local-validation-2026-10-05.json) records complete input hashes, explicit settings, commands, status codes, report hashes, observation times, and the remaining bounds.

- Tool: official [`contentauth/c2patool` v0.28.1](https://github.com/contentauth/c2patool/releases/tag/v0.28.1), tag commit `4b479f6c681653ce5c48f24b03eb11d9394b26bc`. Linux archive SHA256 `66f6fadff747a6f22e7473663dd5b35a358ddc9f43eadc8993414e1bae98de61` matches the GitHub release asset digest. Executable SHA256 `3f76d680685a1387e2d3b7d13ba18f069d88dc1a05f096d0b7a4995b2633ee13`; actual `--version`: `c2patool 0.28.1`. Release Cargo.lock pins C2PA SDK `0.91.1`.
- Original and absent controls: [`contentauth/c2pa-rs` fixtures](https://github.com/contentauth/c2pa-rs/tree/e4f63a233c3b5bd6f5fcd308a0d0db95ab35eae4/sdk/tests/fixtures), commit `e4f63a233c3b5bd6f5fcd308a0d0db95ab35eae4`. `video1.mp4`: 828571 bytes, SHA256 `b310ba339ca89950e3c921754566d5e44ef52c45dd1ddbbfe1383c8e7761cfd0`. `video1_no_manifest.mp4`: 797909 bytes, SHA256 `e5acfbfbb0db93b1551520f2f692630dabd507555adc9386aa9595acf261d8c9`.
- Official signer and TSA lists: [`c2pa-org/conformance-public`](https://github.com/c2pa-org/conformance-public/tree/3573be509a793a989f093df4f86744a3632f6155/trust-list), commit `3573be509a793a989f093df4f86744a3632f6155`. Signer PEM SHA256 `75cacc98b79ecac33713c7ecfb58d4a0ef383f3c1f886e7409f9e37e8664aea5`; TSA PEM SHA256 `c688d3555f4a2f1f8d663472bbd37888ff234abdd234c25934c0f9292e4eb5c9`. List issue times are 2026-08-05T00:11:39Z and 2026-08-05T00:11:47Z; next updates are one year later. These are frozen retrieved inputs, not an independent verification of the LoTE signature or current revocation state.
- Test-only signer roots: the same pinned SDK's `certs/trust/test_cert_root_bundle.pem`, SHA256 `c67864bf6bae91c7df0abbef274abd1b03116063093ec225a8391d744f8356fc`. The upstream certificate README explicitly identifies test/sample certificates. The extracted public leaf subject contains `O=C2PA Test Signing Cert, OU=FOR TESTING_ONLY, CN=C2PA Signer`; leaf SHA256 `6fb5eddb353a82fa8720b1d54a4925eaa20e128b10cc4b3fa4d3e9e920c04001`.

The fixture checkout has the repository's dual MIT/Apache2 distribution licenses, recorded by URL and hash. No media-specific original-footage attribution or narrower license notice was found. This is a repository distribution-license basis; original-footage ownership/attribution and a Creative Commons license have **not** been independently established. No media is included in this change.

## Actual validation

Fifteen actual tool executions covered five assets under three explicit settings files. Each invocation ran in a separate `bwrap --unshare-net` namespace, with no external route. No provider calls, signing, account access, Docker, or deployment occurred. Original input SHA256 values were checked after validation.

All three policies explicitly set:

```json
{"verify_after_reading":true,"verify_trust":true,"verify_timestamp_trust":true,"ocsp_fetch":false,"remote_manifest_fetch":false}
```

The `trust.anchors` array is either empty, the frozen official signer and TSA PEMs, or the frozen SDK **test-only** signer bundle plus the official TSA PEM. Each anchor declares `trust_kind` (`manifest` or `tsa`), a descriptive `trust_uri`, and its PEM data. No allowed-list shortcut, user default settings, or private signing key is used. The exact settings hashes and anchor hashes are in the receipt.

| Asset | Manifest | Claim signature / covered BMFF hash | Official signer policy | SDK test signer policy | Tool exit |
|---|---|---|---|---|---|
| Original | 2 manifests | `claimSignature.validated`; `assertion.bmffHash.match` | `signingCredential.untrusted` | `signingCredential.trusted` | 0 |
| Byte-identical copy | 2 manifests | Same as original | Untrusted | Test-policy trusted | 0 |
| One covered `mdat` byte changed | 2 manifests retained | Signature remains valid; `assertion.bmffHash.mismatch` | Untrusted, invalid binding | Test signer trusted, invalid binding | **0** |
| Upstream absent control | No claim found | Not applicable | Absent | Absent | 1 |
| Owned FFmpeg stream-copy remux | No claim found | Not applicable | Absent | Absent | 1 |

The tamper changed byte offset 92286 with XOR 1 in an owned copy of the first `mdat` payload. The original was untouched. FFmpeg 7.1.5 performed `-map 0 -c copy` on a separate owned output. Both exact commands and derivative hashes are recorded.

Two findings matter for any future adapter. First, invalid assets can exit **0**, so process success cannot admit an asset. Second, the original under the explicit official signer policy has aggregate `validation_state: "Valid"` **and** active failure `signingCredential.untrusted`. Cryptographic binding and signer trust are separate axes. A future policy must inspect relevant validation codes under its frozen, explicitly enabled trust settings; aggregate `Valid`, disabled trust, or an exit code cannot establish trusted admission.

The original also reports `signingCredential.ocsp.skipped`, `assertion.bmffHash.additionalExclusionsPresent`, and `timeStamp.trusted` with the explanation **“legacy timestamp cert trusted”**. Revocation is unknown; the study validates the declared assertion and one covered-payload tamper, not exhaustive coverage of every file byte. The timestamp result must not be presented as an independently proven strict official-list-only TSA match. Signed test names, author metadata, and claim contents are assertions, not content truth, verified legal identity, or verified media licensing.

## Canonical local source readback

Exact main source `5ae653179e780c80c1d79324caae52c5bac2b4e1` ran its actual `file_fingerprint`, `probe_video`, and `video_duration` helpers against all five public/owned MP4s in the isolated namespace. All returned 2-second MP4/H264/AAC metadata; a second read matched the actual cache, and inputs were unchanged. This is normal local probe/cache compatibility only. `file_fingerprint` is `size:mtime_ns`, not a cryptographic identity. No downloader, social URL, model, full pipeline, or C2PA product integration was exercised.

The first helper invocation hit an environmental error because its readonly sandbox had no writable temporary directory. Retrying with an owned writable `TMPDIR` retained network isolation and passed; this was not a source failure.

## Reproduction and remaining acceptance

Use only the fixed public URLs/hashes in the receipt. Verify the archive's digest before extraction. Create the three explicit settings files by inserting the recorded public PEM data into `trust.anchors`; compare each resulting settings hash. For each original/control/owned derivative, run:

```sh
bwrap --unshare-net --ro-bind / / --dev /dev --proc /proc \
  --bind "$STUDY" "$STUDY" \
  "$STUDY/tool/c2patool/c2patool" \
  --settings "$STUDY/offline-official.json" --detailed "$STUDY/fixtures/video1.mp4"
```

Repeat with `offline-empty.json` and `offline-test.json`, recording stdout/stderr and checking the actual status codes rather than only process exit. The machine receipt contains all fifteen exact observed command/result pairs with `$STUDY` substituting the owned study directory. Raw reports are retained in the audit artifact, not media.

This fills the missing real-tool cryptographic readback/tamper/absence receipts. It does **not** supply a production/conforming-signer trusted MP4 with independently established artifact-specific original-footage attribution, a same-source supported-platform derivative, preservation across arbitrary transforms, or measured user time-saving. Those inputs remain concrete acceptance gaps. The supported conclusion is a possible local-original inspection spike with explicit absent/unknown/untrusted states; automatic social-URL evidence admission and BUILD remain unproved. Issue #20 should remain open for the owner's research decision.
