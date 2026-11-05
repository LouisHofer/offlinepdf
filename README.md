# Offline PDF

Offline PDF Komprimierer & Merger is an Android app for compressing, merging,
and editing PDF files locally on-device. It is optimized for ARM64 Android
phones, OPPO-style scoped storage behavior, and e-ink tablets such as the Onyx
Boox Max Lumi.

The Android build targets Android 15 (API 35), uses NDK 28c for 16 KB
memory-page compatibility, and does not request storage or network permissions.

## Features

- Smart PDF compression with optional MB/percentage targets and a Ghostscript
  default profile when no target is entered
- Visible compression progress and selectable output folder
- PDF merge workflow with original page sizes or A3, A4, and A5 fitting
- Page removal by ranges such as `1,3,5-7`
- Android-friendly file selection with fallback browser
- Local-only processing; PDFs are not uploaded
- Storage Access Framework integration without broad file access
- Backups and cleartext network traffic disabled

## Project overview

Offline PDF is an Android app for reducing PDF file size and combining PDF
files locally on the device. It is designed to work offline without uploads,
without account creation, and without broad storage permissions.

## Features

- PDF compression with target size or percentage-based reduction
- Merge multiple PDFs into one file
- Page preview, removal, and reordering
- Output formatting for A3, A4, and A5 pages
- Local-only processing with no internet dependency
- Android-friendly file selection and scoped storage support

## Important files

- `main.py` - app entry point
- `pdf_compressor_kivy_app.py` - main UI and PDF logic
- `android_pdf_compress.py` - native Ghostscript bridge
- `buildozer.spec` - Android build configuration
- `PRIVACY_POLICY.md` - privacy policy

## License

The app code is MIT licensed. See `LICENSE`.

Third-party dependencies keep their own licenses. The bundled Ghostscript
library is licensed under the GNU AGPL; source code and licensing information
must remain available alongside distributed builds.
