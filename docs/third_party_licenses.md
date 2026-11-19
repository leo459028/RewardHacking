# Third-Party Software in the Published Images

> **Applies to**: `ghcr.io/leo459028/rewardhacking` and `ghcr.io/leo459028/rewardhacking-sandbox-python`, from `v1.6.2`
> **Project license**: Apache License 2.0 ([`LICENSE`](../LICENSE), also at `/app/LICENSE` in the main image)

The published images contain open-source software from other projects: a Debian base system, the Python interpreter and Python packages. Each keeps its own license. This page lists where the license texts are inside the images and how to obtain the source code where a license asks for it. It describes `v1.6.2`; the SBOM attached to each image records the exact components of that build. This page is not legal advice.

---

## What Each Image Contains

| Component | Main image (`rewardhacking`) | Sandbox image (`rewardhacking-sandbox-python`) |
| :--- | :--- | :--- |
| Debian 13 base system | 87 packages | 87 packages |
| Python interpreter | 3.12.16 (official `python:3.12-slim` image, pinned by digest) | same |
| Python packages | 53, in `/opt/venv` | 6 (`pytest`, `coverage` and their dependencies), in `/usr/local/lib/python3.12/site-packages` |
| This project | `/app` (Apache-2.0) | none |

## Where the License Texts Are

- **Debian packages**: every package keeps its copyright and license file at `/usr/share/doc/<package>/copyright`.
- **Python interpreter**: `/usr/local/lib/python3.12/LICENSE.txt` (Python Software Foundation License).
- **Python packages**: each package's `*.dist-info` directory holds its license files (`LICENSE*`, `COPYING*`, or a `licenses/` folder) and, where the project has one, its `NOTICE` file. In `v1.6.2` every Python package ships a license file, and the Apache-2.0 packages with a `NOTICE` file (`prometheus_client`, `requests`, `structlog`) keep it.

List them yourself:

```bash
docker run --rm --entrypoint sh ghcr.io/leo459028/rewardhacking:v1.6.2 -c 'ls /usr/share/doc/*/copyright; ls -d /opt/venv/lib/python3.12/site-packages/*.dist-info'
docker run --rm --entrypoint python ghcr.io/leo459028/rewardhacking:v1.6.2 -c "import importlib.metadata as m; [print(d.metadata['Name'], d.version, d.metadata.get('License-Expression') or d.metadata.get('License') or '') for d in m.distributions()]"
```

## Licenses That Ask for Source Code

Most Python packages use permissive licenses (MIT, BSD, Apache-2.0, PSF). Two kinds of components also ask that recipients can obtain the source code:

- **GPL and LGPL programs in the Debian base system** (for example `bash`, `coreutils` and the GNU C library). The corresponding source of every Debian package version is kept by Debian: look up the package and version from the image's SBOM or with `dpkg-query -W` inside the image, then download the source from [snapshot.debian.org](https://snapshot.debian.org/) or the Debian archive.
- **MPL-2.0 Python packages**: `certifi` (Mozilla's CA certificate bundle) ships as Python source inside the image. `orjson` contains compiled MPL-2.0 code; its source for the exact version is published on [PyPI](https://pypi.org/project/orjson/) and [GitHub](https://github.com/ijl/orjson).

If you cannot obtain a source package this way, open an issue in this repository.

## Checking a Build

Each published image has an SBOM and a provenance attestation:

```bash
docker buildx imagetools inspect ghcr.io/leo459028/rewardhacking:v1.6.2 --format '{{ json .SBOM }}'
```

Python dependencies are resolved when the image is built, so a later build of the same tag can contain other versions; the SBOM of that build is authoritative. Known vulnerabilities in Python dependencies fail a pull request before a release (see [`testing_guide.md`](testing_guide.md)).
