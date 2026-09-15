# NW.js release authentication

`nwjs.asc` is a public-only GnuPG `export-minimal` snapshot retrieved on
2026-09-08 from:

https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x1E8BEE8D5B0C4CBCD6D19E2678680FA9E21BB40A

The full primary fingerprint matches the upstream release-verification guide:
https://raw.githubusercontent.com/nwjs/nw.js/main/README.md

- Primary: `1E8BEE8D5B0C4CBCD6D19E2678680FA9E21BB40A`.
- Signing subkey: `B50BE4F12D79AC15671635287F00160778FBAEBA`.
- At retrieval, GnuPG reported neither expired nor revoked. Primary expiry:
  Unix timestamp `1814548487`; signing subkey expiry: `1814548566`.

The detached signatures and exact manifest bytes at the following URLs were
verified with GnuPG and this bundled key:

- https://dl.nwjs.io/v0.90.0/SHASUMS256.txt.asc
- https://dl.nwjs.io/v0.115.0/SHASUMS256.txt.asc

Their manifests are the same URLs without `.asc`. The signature at
https://dl.nwjs.io/v0.109.1/SHASUMS256.txt.asc returned HTTP 404.

Ship `runtime/keys/*.asc` as package data for `box`. Updating this snapshot
requires checking the full fingerprint against upstream and inspecting GnuPG
validity, expiry and revocations again. Never bundle private keys.

## Policy and limits

Before extracting a new or cached NW.js tar, fetch the detached signature and
manifest over official-host-only HTTPS, verify with `/usr/bin/gpg` in a fresh
temporary home, then hash the pinned archive descriptor. No user keyring,
runtime keyserver lookup, or cached trust metadata is used. Only a signature
HTTP 404 permits HTTPS-only installation, with an explicit stderr warning.
All other metadata, signature and hash failures stop installation.

Signature size is limited to 16 KiB; manifest size to 4 MiB; each GPG command
has a 30-second timeout. Expired/revoked keys and signatures are rejected.
The bundled snapshot cannot discover subsequent revocations without a package
update. A compromised official HTTPS service can suppress a signature with 404
under the requested compatibility policy. Authentication does not protect
against a process with the same user's write access modifying an open tar
concurrently, nor does it guarantee integrity of an already installed runtime.
EasyRPG remains HTTPS-only where upstream publishes no signatures; no private
checksum catalog is introduced.
