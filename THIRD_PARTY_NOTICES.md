# Third-party notices

Research Companion uses the following optional PDF components for the
Windows/CPU PDF Library. Their licenses are not the license of Research
Companion itself; the corresponding notices must remain available in source
and release distributions.

## RapidOCR ONNX Runtime

- Project: https://github.com/RapidAI/RapidOCR
- Package: `rapidocr-onnxruntime` (CPU ONNX Runtime distribution)
- License: Apache License 2.0 for the package code.
- The OCR model weights are attributed separately by RapidOCR/Baidu. The
  bundled model package and its notices must be reviewed when changing model
  versions.

## pypdf

- Project: https://github.com/py-pdf/pypdf
- License: BSD-3-Clause.

## pypdfium2 / PDFium

- Project: https://github.com/pypdfium2-team/pypdfium2
- pypdfium2 is distributed under Apache-2.0/BSD-3-Clause terms; PDFium and
  the binary wheel may contain additional permissive dependency notices.
  The wheel's `LICENSES`/build notices must be shipped with binary releases.

## pdf-translator compatibility

The project at https://github.com/discus0434/pdf-translator is licensed under
CC BY-NC 4.0. Its source code, model files, and Docker assets are not copied
into this repository. Research Companion implements an independent CPU path
with a similar side-by-side reading experience. If the upstream translator
is used as an external service in the future, its CC BY-NC 4.0 attribution and
license conditions must remain attached to that optional integration.
