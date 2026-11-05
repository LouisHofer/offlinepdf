from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput
from kivy.uix.progressbar import ProgressBar
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.uix.image import Image
from kivy.uix.behaviors import ButtonBehavior
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.core.window import Window
import threading
import os
import tempfile
# Ghostscript is invoked without shell and with fixed arguments.
import subprocess  # nosec B404
import shutil
import hashlib
import json
import time
import re
from pathlib import Path

from plyer import filechooser, storagepath
from kivy.utils import platform

# Compression result caching
CACHE_DIR = os.path.join(os.path.dirname(__file__), '.compression_cache')
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_VERSION = 5
MAX_TARGET_SIZE_MB = 500
MAX_INPUT_SIZE_BYTES = 500 * 1024 * 1024
ANDROID_PICK_PDF_REQUEST = 8101
ANDROID_PICK_MERGE_PDF_REQUEST = 8102
ANDROID_PICK_OUTPUT_DIR_REQUEST = 8103
ACTION_BUTTON_COLOR = (0.84, 0.94, 0.99, 1)


class WhiteSpinnerOption(SpinnerOption):
    """Bright dropdown row matching the app's white interface."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.background_normal = ''
        self.background_down = ''
        self.background_color = (1, 1, 1, 1)
        self.color = (0.08, 0.08, 0.08, 1)
        self.font_size = '18sp'
        self.height = dp(54)


class PreviewImage(ButtonBehavior, Image):
    """Image widget that opens a larger page preview when tapped."""


def is_path_inside(child_path, parent_path):
    """Return True when child_path resolves inside parent_path."""
    try:
        child = Path(child_path).resolve()
        parent = Path(parent_path).resolve()
        return child == parent or parent in child.parents
    except Exception:
        return False


def is_valid_pdf_path(file_path):
    """Lightweight validation for user-selected local PDFs."""
    try:
        if not file_path or not os.path.isfile(file_path):
            return False
        if not file_path.lower().endswith('.pdf'):
            return False
        file_size = os.path.getsize(file_path)
        if file_size <= 5 or file_size > MAX_INPUT_SIZE_BYTES:
            return False
        with open(file_path, 'rb') as f:
            return f.read(5) == b'%PDF-'
    except Exception:
        return False


def safe_unlink(path):
    """Remove a file if it exists; ignore cleanup failures."""
    try:
        if path and os.path.isfile(path):
            os.unlink(path)
    except Exception as e:
        print(f"[DEBUG] Cleanup failed for {path}: {e}")


def copy_java_input_stream(
    input_stream,
    output_file,
    buffer_size=256 * 1024,
    max_bytes=MAX_INPUT_SIZE_BYTES,
):
    """Copy a Java InputStream to a binary Python file object."""
    buffer = bytearray(buffer_size)
    empty_reads = 0
    total_bytes = 0
    while True:
        bytes_read = input_stream.read(buffer)
        if bytes_read == -1:
            return
        if bytes_read == 0:
            empty_reads += 1
            if empty_reads >= 3:
                raise OSError('Android input stream stopped making progress')
            continue
        if bytes_read < -1 or bytes_read > len(buffer):
            raise OSError(f'Android input stream returned invalid byte count: {bytes_read}')
        empty_reads = 0
        total_bytes += bytes_read
        if total_bytes > max_bytes:
            raise ValueError('Die ausgewählte PDF ist größer als 500 MB')
        output_file.write(buffer[:bytes_read])


def get_file_hash(file_path):
    """Get SHA256 hash of file content."""
    hash_sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_sha256.update(chunk)
    return hash_sha256.hexdigest()

def get_cache_key(file_hash, max_size_mb, target_reduction):
    """Generate cache key from file hash and compression settings."""
    return (
        f"v{CACHE_VERSION}_{file_hash}_"
        f"{max_size_mb or 'None'}_{target_reduction or 'None'}"
    )

def load_cache_result(cache_key):
    """Load cached compression result if available and not expired."""
    cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
    if not os.path.exists(cache_file):
        return None

    try:
        with open(cache_file, 'r') as f:
            data = json.load(f)

        # Check if cache is still valid (24 hours)
        if time.time() - data['timestamp'] > 24 * 3600:
            os.remove(cache_file)
            return None

        cached_file_path = data.get('cached_file_path')
        if not cached_file_path or not is_path_inside(cached_file_path, CACHE_DIR):
            return None

        return data
    except Exception:
        return None

def save_cache_result(cache_key, result_data):
    """Save compression result to cache."""
    cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
    try:
        result_data['timestamp'] = time.time()
        with open(cache_file, 'w') as f:
            json.dump(result_data, f)

        # Clean up old cache entries to prevent unlimited growth
        cleanup_cache()
    except Exception as e:
        print(f"[DEBUG] Cache save failed: {e}")

def cleanup_cache():
    """Clean up old cache entries to keep cache size manageable."""
    try:
        # Get all cache files with their timestamps
        cache_files = []
        for filename in os.listdir(CACHE_DIR):
            if filename.endswith('.json'):
                filepath = os.path.join(CACHE_DIR, filename)
                try:
                    with open(filepath, 'r') as f:
                        data = json.load(f)
                    cache_files.append((filepath, data.get('timestamp', 0)))
                except Exception as e:
                    # Remove corrupted cache files
                    print(f"[DEBUG] Removing corrupted cache metadata {filepath}: {e}")
                    os.remove(filepath)

        # Sort by timestamp (oldest first)
        cache_files.sort(key=lambda x: x[1])

        # Keep only the 50 most recent entries
        max_entries = 50
        if len(cache_files) > max_entries:
            files_to_remove = cache_files[:-max_entries]
            for filepath, _ in files_to_remove:
                try:
                    # Remove the JSON metadata file
                    os.remove(filepath)
                    # Also remove the associated PDF file
                    pdf_file = filepath.replace('.json', '.pdf')
                    if os.path.exists(pdf_file):
                        os.remove(pdf_file)
                except Exception as e:
                    print(f"[DEBUG] Cache cleanup failed for {filepath}: {e}")

        # Also check total cache size (keep under 100MB)
        total_size = 0
        all_files = []
        for root, dirs, files in os.walk(CACHE_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                try:
                    size = os.path.getsize(filepath)
                    total_size += size
                    all_files.append((filepath, size, os.path.getmtime(filepath)))
                except Exception as e:
                    print(f"[DEBUG] Cache size scan skipped {filepath}: {e}")

        max_size = 100 * 1024 * 1024  # 100MB
        if total_size > max_size:
            # Sort by modification time (oldest first) and remove oldest files
            all_files.sort(key=lambda x: x[2])
            for filepath, size, _ in all_files:
                if total_size <= max_size:
                    break
                try:
                    os.remove(filepath)
                    total_size -= size
                except Exception as e:
                    print(f"[DEBUG] Cache size cleanup failed for {filepath}: {e}")

    except Exception as e:
        print(f"[DEBUG] Cache cleanup failed: {e}")

try:
    from kivymd.app import MDApp
    KIVYMD_AVAILABLE = True
except Exception as e:
    MDApp = App
    KIVYMD_AVAILABLE = False
    print(f"[INFO] KivyMD not available: {e}")

def analyze_compression_curve(results, target_size=None, target_reduction=None):
    """
    Analyze compression results using statistical methods to find optimal DPI.
    Uses second derivatives to find the 'knee' of the compression curve.
    """
    if len(results) < 3:
        # Not enough data for statistical analysis, return best result
        if target_size:
            suitable_results = [r for r in results if r[1] <= target_size]
            if suitable_results:
                return max(suitable_results, key=lambda x: x[1])
            return min(results, key=lambda x: x[1])
        elif target_reduction:
            return min(results, key=lambda x: abs(x[2] - target_reduction))
        else:
            return max(results, key=lambda x: x[2])  # Max reduction

    # Sort by DPI ascending
    results.sort(key=lambda x: x[3])

    # Extract data points
    dpis = [r[3] for r in results]
    sizes = [r[1] for r in results]
    reductions = [r[2] for r in results]

    # Calculate first derivatives (rate of change)
    first_derivatives = []
    for i in range(1, len(dpis)):
        dpi_diff = dpis[i] - dpis[i-1]
        if dpi_diff == 0:
            continue
        size_change = sizes[i] - sizes[i-1]
        reduction_change = reductions[i] - reductions[i-1]

        # Use size change per DPI as primary metric
        first_derivatives.append(size_change / dpi_diff)

    if len(first_derivatives) < 2:
        # Fallback to simple optimization
        if target_size:
            suitable_results = [r for r in results if r[1] <= target_size]
            if suitable_results:
                return max(suitable_results, key=lambda x: x[1])
            return min(results, key=lambda x: x[1])
        else:
            return max(results, key=lambda x: x[2])

    # Calculate second derivatives (acceleration/deceleration)
    second_derivatives = []
    for i in range(1, len(first_derivatives)):
        deriv_diff = first_derivatives[i] - first_derivatives[i-1]
        second_derivatives.append(deriv_diff)

    # Find the 'knee' - where compression efficiency changes significantly
    # Look for maximum absolute second derivative (biggest change in compression rate)
    max_change_idx = second_derivatives.index(max(second_derivatives, key=abs))

    # The knee is at the point just before this big change
    knee_idx = max_change_idx + 1

    # Select optimal result based on target
    if target_size:
        # Hard file-size limits must be respected. Pick the largest file that
        # fits the limit to preserve as much quality as possible.
        suitable_results = [r for r in results if r[1] <= target_size]
        if suitable_results:
            return max(suitable_results, key=lambda x: x[1])
        return min(results, key=lambda x: x[1])
    elif target_reduction:
        # For reduction targets, prefer results that meet or exceed the target
        # but find the one with minimal overshoot
        suitable_results = [r for r in results if r[2] >= target_reduction]
        if suitable_results:
            # Find the result with reduction closest to target (minimal overshoot)
            return min(suitable_results, key=lambda x: x[2] - target_reduction)
        else:
            # No result meets target, return the best available (highest reduction)
            return max(results, key=lambda x: x[2])
    else:
        # Maximize reduction while preferring results after the knee
        if knee_idx < len(results):
            return max(results[knee_idx:], key=lambda x: x[2])
        else:
            return max(results, key=lambda x: x[2])

def refine_around_optimal(input_path, output_path, optimal_dpi, target_size, temp_files):
    """
    Fine-tune compression around the statistically optimal DPI for better target matching.
    Tests ±5 DPI around optimal point.
    """
    if not target_size:
        return None

    # Test 3 points around optimal: optimal-5, optimal, optimal+5
    test_dpis = [max(18, optimal_dpi - 5), optimal_dpi, min(300, optimal_dpi + 5)]
    best_refined = None

    for dpi in test_dpis:
        if dpi == optimal_dpi:
            continue  # Already tested this one

        pdf_setting = '/ebook' if dpi >= 120 else '/screen'
        candidate_path = f"{output_path}.gs_refine_{dpi}.pdf"
        temp_files.append(candidate_path)

        cmd = [
            'gs',
            '-sDEVICE=pdfwrite',
            '-dCompatibilityLevel=1.4',
            '-dNOPAUSE',
            '-dQUIET',
            '-dBATCH',
            '-dSAFER',
            '-dDetectDuplicateImages=true',
            '-dCompressFonts=true',
            '-dSubsetFonts=true',
            f'-dPDFSETTINGS={pdf_setting}',
            '-dDownsampleColorImages=true',
            f'-dColorImageResolution={dpi}',
            '-dDownsampleGrayImages=true',
            f'-dGrayImageResolution={dpi}',
            '-dDownsampleMonoImages=true',
            f'-dMonoImageResolution={max(72, dpi)}',
            '-dAutoFilterColorImages=false',
            '-dAutoFilterGrayImages=false',
            '-dColorImageFilter=/FlateEncode',
            '-dGrayImageFilter=/FlateEncode',
            '-dMonoImageFilter=/CCITTFaxEncode',
            f'-sOutputFile={candidate_path}',
            input_path
        ]

        try:
            result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # nosec B603
            if result.returncode == 0 and os.path.exists(candidate_path):
                candidate_size = os.path.getsize(candidate_path)
                input_size = os.path.getsize(input_path)
                reduction = ((input_size - candidate_size) / input_size * 100) if input_size > 0 else 0

                print(f"[INFO] Refined test ({dpi}dpi): {format_size(candidate_size)} ({reduction:.1f}% reduction)")

                if candidate_size > target_size:
                    safe_unlink(candidate_path)
                    continue

                if best_refined is None or candidate_size > best_refined[1]:
                    if best_refined and os.path.exists(best_refined[0]):
                        safe_unlink(best_refined[0])
                    best_refined = (candidate_path, candidate_size, reduction, dpi)
                else:
                    safe_unlink(candidate_path)
        except Exception as e:
            print(f"[WARN] Refined compression failed for {dpi}dpi: {e}")
            if os.path.exists(candidate_path):
                safe_unlink(candidate_path)

    return best_refined

# On Android: use the bundled native Ghostscript library.

compress_pdf_android = None
merge_pdfs_android = None
render_pdf_thumbnails_android = None
extract_pdf_page_android = None
GHOSTSCRIPT_JNI_AVAILABLE = False
if platform == 'android':
    try:
        from android_pdf_compress import (
            compress_pdf_android,
            extract_pdf_page_android,
            merge_pdfs_android,
            render_pdf_thumbnails_android,
            GHOSTSCRIPT_JNI_AVAILABLE,
        )
        print(
            "[INFO] Android native compression module: "
            f"Ghostscript={GHOSTSCRIPT_JNI_AVAILABLE}"
        )
    except Exception as e:
        print(f"[WARNING] Android compression module not found: {e}")

    try:
        from jnius import autoclass
        from android import activity
    except Exception as e:
        print(f"[WARNING] Jnius/Android not available: {e}")
def format_size(bytes_size):
    """Convert bytes to human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} TB"


def calculate_target_size(input_size, max_size_mb=None, target_reduction=None):
    """Calculate effective target bytes from MB limit and reduction target.

    If both are provided, both must be satisfied, so we take the smaller size target.
    """
    targets = []
    if max_size_mb is not None and max_size_mb > 0:
        targets.append(int(max_size_mb * 1024 * 1024))
    if target_reduction is not None and target_reduction > 0:
        targets.append(int(input_size * (1 - target_reduction / 100.0)))
    return min(targets) if targets else None


def sizes_nearly_equal(left, right):
    return abs(left - right) <= max(4096, int(max(left, right) * 0.005))


def parse_inkcov_color_mode(output):
    """Return 'color' for clearly non-neutral ink coverage, else uncertain."""
    coverage_rows = []
    for line in output.splitlines():
        match = re.search(
            r'(\d+(?:\.\d+)?)\s+'
            r'(\d+(?:\.\d+)?)\s+'
            r'(\d+(?:\.\d+)?)\s+'
            r'(\d+(?:\.\d+)?)\s+CMYK',
            line,
        )
        if match:
            coverage_rows.append(tuple(float(value) for value in match.groups()))

    if not coverage_rows:
        return 'uncertain'

    has_color = any(
        max(cyan, magenta, yellow) - min(cyan, magenta, yellow) > 0.01
        for cyan, magenta, yellow, _black in coverage_rows
    )
    return 'color' if has_color else 'uncertain'


def classify_ppm_color_mode(file_paths):
    """Classify low-resolution PPM previews by RGB channel differences."""
    colored_pixels = 0
    sampled_pixels = 0

    for file_path in file_paths:
        with open(file_path, 'rb') as handle:
            data = handle.read()

        tokens = []
        index = 0
        while len(tokens) < 4 and index < len(data):
            while index < len(data) and data[index] in b' \t\r\n':
                index += 1
            if index < len(data) and data[index] == ord('#'):
                while index < len(data) and data[index] not in b'\r\n':
                    index += 1
                continue
            start = index
            while index < len(data) and data[index] not in b' \t\r\n':
                index += 1
            tokens.append(data[start:index])

        if len(tokens) != 4 or tokens[0] != b'P6' or tokens[3] != b'255':
            return 'color'
        if index < len(data) and data[index] == ord('\r'):
            index += 1
            if index < len(data) and data[index] == ord('\n'):
                index += 1
        elif index < len(data) and data[index] in b' \t\n':
            index += 1

        pixels = data[index:]
        sampled_pixels += len(pixels) // 3
        for offset in range(0, len(pixels) - 2, 3):
            red, green, blue = pixels[offset:offset + 3]
            if max(red, green, blue) - min(red, green, blue) > 6:
                colored_pixels += 1

    if sampled_pixels == 0:
        return 'color'
    allowed_colored_pixels = max(2, int(sampled_pixels * 0.0002))
    return 'color' if colored_pixels > allowed_colored_pixels else 'gray'


def detect_pdf_color_mode_cli(input_path):
    """Use Ghostscript ink coverage to conservatively detect grayscale PDFs."""
    if not shutil.which('gs'):
        return 'color'

    cmd = [
        'gs',
        '-dSAFER',
        '-dNOPAUSE',
        '-dBATCH',
        '-dQUIET',
        '-sDEVICE=inkcov',
        '-o',
        '-',
        input_path,
    ]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # nosec B603
        if result.returncode != 0:
            return 'color'
        inkcov_mode = parse_inkcov_color_mode(f'{result.stdout}\n{result.stderr}')
        if inkcov_mode == 'color':
            print('[INFO] Ghostscript color analysis: color')
            return 'color'

        with tempfile.TemporaryDirectory(prefix='pdf-color-check-') as temp_dir:
            output_pattern = os.path.join(temp_dir, 'page-%04d.ppm')
            preview_cmd = [
                'gs',
                '-dSAFER',
                '-dNOPAUSE',
                '-dBATCH',
                '-dQUIET',
                '-sDEVICE=ppmraw',
                '-dFirstPage=1',
                '-dLastPage=3',
                '-r12',
                f'-sOutputFile={output_pattern}',
                input_path,
            ]
            preview = subprocess.run(
                preview_cmd,
                check=False,
                capture_output=True,
                text=True,
            )  # nosec B603
            if preview.returncode != 0:
                return 'color'
            preview_files = sorted(
                os.path.join(temp_dir, name)
                for name in os.listdir(temp_dir)
                if name.endswith('.ppm')
            )
            mode = classify_ppm_color_mode(preview_files)
        print(f'[INFO] Ghostscript color analysis: {mode}')
        return mode
    except Exception as e:
        print(f'[WARNING] Ghostscript color analysis failed: {e}')
        return 'color'


def ghostscript_color_arguments(color_mode):
    """Return conservative size-saving color options."""
    common = [
        '-dConvertCMYKImagesToRGB=true',
        '-dPreserveHalftoneInfo=false',
        '-dPreserveMarkedContent=false',
    ]
    if color_mode == 'gray':
        return ['-sColorConversionStrategy=Gray', *common]
    return common


def compress_pdf_with_ghostscript_cli(
    input_path,
    output_path,
    max_size_mb=None,
    target_reduction=None,
    progress_callback=None,
):
    """Compress with a binary DPI search against the requested target."""
    if platform == 'android':
        return None

    if not shutil.which('gs'):
        return None

    input_size = os.path.getsize(input_path)
    target_size = calculate_target_size(input_size, max_size_mb=max_size_mb, target_reduction=target_reduction)
    if progress_callback:
        progress_callback(0.02, 'Farben der PDF werden analysiert')
    color_mode = detect_pdf_color_mode_cli(input_path)
    color_arguments = ghostscript_color_arguments(color_mode)
    if target_size is None:
        cmd = [
            'gs',
            '-sDEVICE=pdfwrite',
            '-dCompatibilityLevel=1.4',
            '-dNOPAUSE',
            '-dQUIET',
            '-dBATCH',
            '-dSAFER',
            '-dPDFSETTINGS=/ebook',
            '-dDetectDuplicateImages=true',
            '-dCompressFonts=true',
            '-dSubsetFonts=true',
            *color_arguments,
            f'-sOutputFile={output_path}',
            input_path,
        ]
        if progress_callback:
            progress_callback(0.15, 'Automatische Komprimierung wird angewendet')
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # nosec B603
        if (
            result.returncode == 0
            and os.path.isfile(output_path)
            and os.path.getsize(output_path) >= input_size
        ):
            shutil.copy2(input_path, output_path)
            if progress_callback:
                progress_callback(
                    1.0,
                    'PDF ist bereits optimal; Originalgrösse beibehalten',
                )
            return True
        if progress_callback:
            progress_callback(1.0, 'Komprimierung abgeschlossen')
        return (
            result.returncode == 0
            and os.path.isfile(output_path)
            and os.path.getsize(output_path) > 0
        )
    print(f"[INFO] Ghostscript effective target: {format_size(target_size)}")

    min_dpi = 36
    max_dpi = 300
    max_attempts = 9
    low_dpi = min_dpi
    high_dpi = max_dpi
    attempts = 0
    best_fitting = None
    smallest_result = None
    temp_files = []
    plateau_hits = 0

    while low_dpi <= high_dpi and attempts < max_attempts:
        dpi = (low_dpi + high_dpi) // 2
        attempts += 1
        candidate_path = f"{output_path}.gs_binary_{attempts}_{dpi}.pdf"
        temp_files.append(candidate_path)
        if progress_callback:
            progress_callback(
                (attempts - 1) / max_attempts,
                f'Optimierung: {dpi} dpi, Schritt {attempts}/{max_attempts}',
            )

        cmd = [
            'gs',
            '-sDEVICE=pdfwrite',
            '-dCompatibilityLevel=1.4',
            '-dNOPAUSE',
            '-dQUIET',
            '-dBATCH',
            '-dSAFER',
            '-dDetectDuplicateImages=true',
            '-dCompressFonts=true',
            '-dSubsetFonts=true',
            *color_arguments,
            '-dPassThroughJPEGImages=false',
            '-dPassThroughJPXImages=false',
            '-dDownsampleColorImages=true',
            '-dColorImageDownsampleType=/Bicubic',
            '-dColorImageDownsampleThreshold=1.0',
            f'-dColorImageResolution={dpi}',
            '-dDownsampleGrayImages=true',
            '-dGrayImageDownsampleType=/Bicubic',
            '-dGrayImageDownsampleThreshold=1.0',
            f'-dGrayImageResolution={dpi}',
            '-dDownsampleMonoImages=true',
            '-dMonoImageDownsampleType=/Subsample',
            '-dMonoImageDownsampleThreshold=1.0',
            f'-dMonoImageResolution={max(72, dpi)}',
            '-dAutoFilterColorImages=false',
            '-dAutoFilterGrayImages=false',
            '-dColorImageFilter=/DCTEncode',
            '-dGrayImageFilter=/DCTEncode',
            '-dMonoImageFilter=/CCITTFaxEncode',
            '-dJPEGQ=82',
            f'-sOutputFile={candidate_path}',
            input_path
        ]

        try:
            result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # nosec B603
            if result.returncode != 0:
                safe_unlink(candidate_path)
                high_dpi = dpi - 1
                continue
        except Exception as e:
            print(f"[DEBUG] Ghostscript binary step {attempts} failed: {e}")
            safe_unlink(candidate_path)
            high_dpi = dpi - 1
            continue

        if not os.path.exists(candidate_path):
            high_dpi = dpi - 1
            continue

        candidate_size = os.path.getsize(candidate_path)
        reduction = ((input_size - candidate_size) / input_size * 100) if input_size > 0 else 0
        current_result = (candidate_path, candidate_size, reduction, dpi)
        print(
            f"[INFO] GS binary {attempts}: {dpi}dpi -> "
            f"{format_size(candidate_size)} ({reduction:.1f}% reduction)"
        )

        if smallest_result is None or candidate_size < smallest_result[1]:
            smallest_result = current_result

        if candidate_size <= target_size:
            previous_best = best_fitting
            if (
                candidate_size < input_size
                and (
                best_fitting is None
                or dpi > best_fitting[3]
                or (
                    dpi == best_fitting[3]
                    and candidate_size > best_fitting[1]
                )
                )
            ):
                best_fitting = current_result
            if (
                previous_best is not None
                and dpi > previous_best[3]
                and sizes_nearly_equal(candidate_size, previous_best[1])
            ):
                plateau_hits += 1
            else:
                plateau_hits = 0
            low_dpi = dpi + 1
            if (
                target_size - candidate_size <= target_size * 0.02
                or plateau_hits >= 1
            ):
                break
        else:
            plateau_hits = 0
            high_dpi = dpi - 1

    # Clean up temp files and finalize
    best_result = best_fitting or smallest_result
    if best_result and best_result[1] >= input_size:
        shutil.copy2(input_path, output_path)
        for temp_path in temp_files:
            safe_unlink(temp_path)
        if progress_callback:
            progress_callback(
                1.0,
                'PDF ist bereits optimal; Originalgrösse beibehalten',
            )
        return True
    if best_result and os.path.exists(best_result[0]):
        try:
            os.replace(best_result[0], output_path)
            total_reduction = best_result[2]
            print(
                f"[INFO] GS binary selected: {best_result[3]}dpi, "
                f"{format_size(best_result[1])} ({total_reduction:.1f}% reduction)"
            )

            # Clean up other temp files
            for temp_path in temp_files:
                if temp_path != output_path and os.path.exists(temp_path):
                    safe_unlink(temp_path)
            if progress_callback:
                progress_callback(1.0, 'Komprimierung abgeschlossen')
            return True
        except Exception as e:
            print(f"[DEBUG] Finalizing Ghostscript result failed: {e}")

    # Clean up all temp files on failure
    for temp_path in temp_files:
        if os.path.exists(temp_path):
            safe_unlink(temp_path)
    return False


def compress_pdf(
    input_path,
    output_path,
    max_size_mb=None,
    resize=False,
    dpi=None,
    target_reduction=None,
    progress_callback=None,
):
    """Compress PDF using best available method.

    Priority:
    1. Check cache for similar files
    2. Android: native Ghostscript (best compression)
    3. Desktop: Ghostscript CLI (best compression)
    """

    # Check cache first
    input_size = os.path.getsize(input_path)
    try:
        file_hash = get_file_hash(input_path)
        cache_key = get_cache_key(file_hash, max_size_mb, target_reduction)
        cached_result = load_cache_result(cache_key)

        if cached_result:
            if (
                max_size_mb is None
                and target_reduction is None
                and cached_result.get('output_size', input_size) > input_size
            ):
                cached_result = None

        if cached_result:
            print(f"[INFO] Cache hit! Using cached result: {format_size(cached_result['output_size'])} ({cached_result['reduction']:.1f}% reduction)")
            # Copy cached file to output location
            cached_file = cached_result['cached_file_path']
            if os.path.exists(cached_file):
                shutil.copy2(cached_file, output_path)
                if progress_callback:
                    progress_callback(1.0, 'Gespeichertes Ergebnis verwendet')
                return True
            else:
                print("[WARNING] Cached file missing, proceeding with compression")
    except Exception as e:
        print(f"[WARNING] Cache check failed: {e}")

    compression_success = False

    # On Android, use the bundled Ghostscript C API.
    if platform == 'android' and GHOSTSCRIPT_JNI_AVAILABLE and compress_pdf_android:
        print("[INFO] Compression backend: native Ghostscript")
        try:
            result = compress_pdf_android(
                input_path,
                output_path,
                max_size_mb=max_size_mb,
                target_reduction=target_reduction,
                progress_callback=progress_callback,
            )
            if result:
                compression_success = True
        except Exception as e:
            print(f"[ERROR] Android native Ghostscript failed: {e}")
    elif platform == 'android':
        print("[ERROR] Native Ghostscript is not packaged; compression unavailable")

    # On desktop, use Ghostscript CLI first for stronger compression
    if not compression_success and platform != 'android':
        print("[INFO] Trying desktop Ghostscript compression")
        result = compress_pdf_with_ghostscript_cli(
            input_path,
            output_path,
            max_size_mb=max_size_mb,
            target_reduction=target_reduction,
            progress_callback=progress_callback,
        )
        if result:
            compression_success = True

    # Save result to cache if compression was successful
    if compression_success and os.path.exists(output_path):
        try:
            output_size = os.path.getsize(output_path)
            input_size = os.path.getsize(input_path)
            reduction = ((input_size - output_size) / input_size * 100) if input_size > 0 else 0

            # Store compressed file in cache directory
            cached_file_path = os.path.join(CACHE_DIR, f"{cache_key}.pdf")
            shutil.copy2(output_path, cached_file_path)

            cache_data = {
                'input_size': input_size,
                'output_size': output_size,
                'reduction': reduction,
                'cached_file_path': cached_file_path,
                'max_size_mb': max_size_mb,
                'target_reduction': target_reduction
            }
            save_cache_result(cache_key, cache_data)
            print(f"[INFO] Result cached for future use")
        except Exception as e:
            print(f"[WARNING] Cache save failed: {e}")

    return compression_success


def merge_pdfs_with_ghostscript(input_paths, output_path, page_size=None):
    """Merge PDFs with Ghostscript and optionally fit pages to A3/A4/A5."""
    if platform == 'android':
        if not GHOSTSCRIPT_JNI_AVAILABLE or not merge_pdfs_android:
            return False
        return merge_pdfs_android(input_paths, output_path, page_size=page_size)

    if not shutil.which('gs'):
        return False

    cmd = [
        'gs',
        '-sDEVICE=pdfwrite',
        '-dCompatibilityLevel=1.5',
        '-dNOPAUSE',
        '-dQUIET',
        '-dBATCH',
        '-dSAFER',
    ]
    if page_size:
        cmd.extend([
            f'-sPAPERSIZE={page_size.lower()}',
            '-dFIXEDMEDIA',
            '-dPDFFitPage',
        ])
    cmd.extend([f'-sOutputFile={output_path}', *input_paths])
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # nosec B603
    return (
        result.returncode == 0
        and os.path.isfile(output_path)
        and os.path.getsize(output_path) > 0
    )


def render_pdf_thumbnails(input_path, output_pattern, dpi=28, progress_callback=None):
    """Render all pages to small PNG files for the interactive merge editor."""
    if platform == 'android':
        PdfRenderer = autoclass('android.graphics.pdf.PdfRenderer')
        PdfRendererPage = autoclass('android.graphics.pdf.PdfRenderer$Page')
        ParcelFileDescriptor = autoclass('android.os.ParcelFileDescriptor')
        JavaFile = autoclass('java.io.File')
        FileOutputStream = autoclass('java.io.FileOutputStream')
        Bitmap = autoclass('android.graphics.Bitmap')
        BitmapConfig = autoclass('android.graphics.Bitmap$Config')
        CompressFormat = autoclass('android.graphics.Bitmap$CompressFormat')
        Canvas = autoclass('android.graphics.Canvas')

        descriptor = None
        renderer = None
        generated = []
        try:
            descriptor = ParcelFileDescriptor.open(
                JavaFile(input_path),
                ParcelFileDescriptor.MODE_READ_ONLY,
            )
            renderer = PdfRenderer(descriptor)
            page_count = renderer.getPageCount()
            for page_index in range(page_count):
                page = None
                bitmap = None
                output_stream = None
                try:
                    page = renderer.openPage(page_index)
                    scale = min(
                        240.0 / max(1, page.getWidth()),
                        340.0 / max(1, page.getHeight()),
                    )
                    width = max(1, int(page.getWidth() * scale))
                    height = max(1, int(page.getHeight() * scale))
                    bitmap = Bitmap.createBitmap(
                        width,
                        height,
                        BitmapConfig.ARGB_8888,
                    )
                    Canvas(bitmap).drawARGB(255, 255, 255, 255)
                    page.render(
                        bitmap,
                        None,
                        None,
                        PdfRendererPage.RENDER_MODE_FOR_DISPLAY,
                    )
                    output_path = output_pattern % (page_index + 1)
                    output_stream = FileOutputStream(output_path)
                    if not bitmap.compress(
                        CompressFormat.PNG,
                        90,
                        output_stream,
                    ):
                        raise RuntimeError('Android konnte die Vorschau nicht speichern.')
                    output_stream.flush()
                    generated.append(output_path)
                    if progress_callback:
                        progress_callback(page_index + 1, page_count, output_path)
                finally:
                    if output_stream is not None:
                        output_stream.close()
                    if page is not None:
                        page.close()
                    if bitmap is not None:
                        bitmap.recycle()
            return generated
        finally:
            if renderer is not None:
                renderer.close()
            if descriptor is not None:
                descriptor.close()

    if not shutil.which('gs'):
        raise RuntimeError('Die PDF-Vorschau ist nicht verfügbar.')
    cmd = [
        'gs',
        '-dNOPAUSE',
        '-dBATCH',
        '-dSAFER',
        '-dQUIET',
        '-sDEVICE=png16m',
        '-dTextAlphaBits=4',
        '-dGraphicsAlphaBits=4',
        f'-r{int(dpi)}',
        f'-sOutputFile={output_pattern}',
        input_path,
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # nosec B603
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or 'Vorschau konnte nicht erstellt werden.')
    output_dir = os.path.dirname(output_pattern)
    prefix = os.path.basename(output_pattern).split('%', 1)[0]
    generated = sorted(
        os.path.join(output_dir, name)
        for name in os.listdir(output_dir)
        if name.startswith(prefix) and name.lower().endswith('.png')
    )
    if progress_callback:
        for page_index, output_path in enumerate(generated, start=1):
            progress_callback(page_index, len(generated), output_path)
    return generated


def extract_pdf_page(input_path, page_number, output_path):
    """Extract one 1-based page as a PDF for a reordered merge."""
    if platform == 'android':
        if not GHOSTSCRIPT_JNI_AVAILABLE or not extract_pdf_page_android:
            return False
        return extract_pdf_page_android(input_path, page_number, output_path)

    if not shutil.which('gs'):
        return False
    cmd = [
        'gs',
        '-dNOPAUSE',
        '-dBATCH',
        '-dSAFER',
        '-dQUIET',
        '-sDEVICE=pdfwrite',
        '-dCompatibilityLevel=1.5',
        f'-dFirstPage={int(page_number)}',
        f'-dLastPage={int(page_number)}',
        f'-sOutputFile={output_path}',
        input_path,
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # nosec B603
    return (
        result.returncode == 0
        and os.path.isfile(output_path)
        and os.path.getsize(output_path) > 0
    )


class PdfCompressorLayout(BoxLayout):
    def __init__(self, **kwargs):
        top_padding = dp(48) if platform == 'android' else dp(14)
        super().__init__(
            orientation='vertical',
            spacing=dp(10),
            padding=[dp(14), top_padding, dp(14), dp(14)],
            **kwargs,
        )

        # Set white background
        from kivy.graphics import Color, Rectangle
        with self.canvas.before:
            Color(1, 1, 1, 1)  # White background
            self.rect = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self._update_rect, pos=self._update_rect)

        self.input_path = None
        self.output_dir = None  # Custom output directory
        self.temp_file_path = None
        self.android_source_name = None
        self.android_source_relative_dir = None
        self.android_output_tree_uri = None
        self.android_output_tree_label = None
        self.compression_running = False
        self.compression_wake_lock = None
        self.merge_list = []  # List of paths for merging
        self.merge_display_names = {}
        self.merge_source_relative_dirs = {}
        self.merge_temp_files = []
        self.merge_mode = False
        self.merge_page_order = []
        self.merge_page_order_dirty = False
        self.merge_preview_dir = None
        self.merge_preview_generation = 0
        self.merge_editor_popup = None
        if platform == 'android':
            try:
                activity.bind(on_activity_result=self._on_android_activity_result)
            except Exception as e:
                print(f"DEBUG: Could not bind Android activity result: {e}")

        self.header = self._make_label(
            'Offline PDF',
            height=dp(38),
            font_size='22sp',
            bold=True,
            color=(0.05, 0.05, 0.05, 1),
        )
        self.add_widget(self.header)

        app_detail = self._make_label(
            'Offline PDF Komprimierer & Merger',
            height=dp(28),
            font_size='15sp',
            color=(0.28, 0.28, 0.28, 1),
        )
        self.add_widget(app_detail)

        self.privacy_btn = self._make_button('Datenschutz', height=dp(44), font_size='16sp')
        self.privacy_btn.bind(on_release=self.show_privacy_policy)
        self.add_widget(self.privacy_btn)

        self.status = self._make_label(
            'PDF auswählen. Ohne Zielwert wird automatisch komprimiert.',
            height=dp(58),
            font_size='16sp',
            color=(0.2, 0.2, 0.2, 1),
        )
        self.add_widget(self.status)

        self.select_btn = self._make_button('PDF auswählen', height=dp(64), font_size='20sp')
        self.select_btn.bind(on_release=self.select_file)
        self.add_widget(self.select_btn)

        size_title = self._make_label('Zielgrösse', height=dp(30), font_size='17sp', bold=True)
        self.add_widget(size_title)

        preset_grid = BoxLayout(orientation='vertical', size_hint_y=None, height=dp(124), spacing=dp(8))
        for preset_row_values in [
            [('0.5 MB', '0.5'), ('1 MB', '1'), ('2 MB', '2')],
            [('3 MB', '3'), ('4 MB', '4'), ('5 MB', '5')],
        ]:
            preset_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(58), spacing=dp(8))
            for label, value in preset_row_values:
                btn = self._make_button(label, height=dp(58), font_size='18sp')
                btn.bind(on_release=lambda instance, mb=value: self._set_max_size_preset(mb))
                preset_row.add_widget(btn)
            preset_grid.add_widget(preset_row)
        self.add_widget(preset_grid)

        self.input_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(58), spacing=dp(8))
        self.max_size_label = self._make_label('MB:', size_hint_x=0.18, font_size='17sp', halign='right')
        self.input_row.add_widget(self.max_size_label)
        self.max_size_input = TextInput(
            text='',
            hint_text='optional',
            multiline=False,
            size_hint_x=0.32,
            font_size='18sp',
            input_filter='float',
            input_type='number',
            background_color=(1, 1, 1, 1),
            foreground_color=(0, 0, 0, 1),
            padding=[dp(10), dp(12), dp(10), dp(8)],
        )
        self.input_row.add_widget(self.max_size_input)
        self.target_label = self._make_label('oder kleiner um %:', size_hint_x=0.28, font_size='16sp', halign='right')
        self.input_row.add_widget(self.target_label)
        self.target_input = TextInput(
            text='',
            hint_text='optional',
            multiline=False,
            size_hint_x=0.22,
            font_size='18sp',
            input_filter='float',
            input_type='number',
            background_color=(1, 1, 1, 1),
            foreground_color=(0, 0, 0, 1),
            padding=[dp(10), dp(12), dp(10), dp(8)],
        )
        self.input_row.add_widget(self.target_input)
        self.add_widget(self.input_row)

        self.compress_btn = self._make_button(
            'Komprimieren',
            height=dp(64),
            font_size='20sp',
            background_color=ACTION_BUTTON_COLOR,
        )
        self.compress_btn.bind(on_release=self.compress)
        self.compress_btn.disabled = True
        self.add_widget(self.compress_btn)

        self.compression_progress = ProgressBar(
            max=100,
            value=0,
            size_hint_y=None,
            height=dp(18),
        )
        self.add_widget(self.compression_progress)

        self.output_dir_btn = self._make_button(
            'Ausgabeordner wählen',
            height=dp(58),
            font_size='18sp',
        )
        self.output_dir_btn.bind(on_release=self.select_output_directory)
        self.add_widget(self.output_dir_btn)

        self.output_dir_label = self._make_label(
            'Ausgabe: gleicher Ordner wie die PDF',
            height=dp(34),
            font_size='14sp',
        )
        self.add_widget(self.output_dir_label)

        # Merge buttons
        merge_title = self._make_label('PDFs zusammenfügen', height=dp(34), font_size='17sp', bold=True)
        self.add_widget(merge_title)

        self.add_merge_btn = self._make_button('PDFs für Merge hinzufügen', height=dp(64), font_size='19sp')
        self.add_merge_btn.bind(on_release=self.add_to_merge)
        self.add_widget(self.add_merge_btn)

        self.edit_merge_btn = self._make_button(
            'Seiten bearbeiten',
            height=dp(58),
            font_size='18sp',
        )
        self.edit_merge_btn.bind(on_release=self.open_merge_page_editor)
        self.edit_merge_btn.disabled = True
        self.add_widget(self.edit_merge_btn)

        merge_action_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(62), spacing=dp(8))
        self.merge_btn = self._make_button(
            'Merge starten',
            height=dp(62),
            font_size='19sp',
            background_color=ACTION_BUTTON_COLOR,
        )
        self.merge_btn.bind(on_release=self.merge_pdfs)
        self.merge_btn.disabled = True
        merge_action_row.add_widget(self.merge_btn)

        self.clear_merge_btn = self._make_button('Liste leeren', height=dp(62), font_size='18sp')
        self.clear_merge_btn.bind(on_release=self.clear_merge_list)
        merge_action_row.add_widget(self.clear_merge_btn)
        self.add_widget(merge_action_row)

        self.merge_status = self._make_label(
            'Merge-Liste leer. Mindestens 2 PDFs hinzufügen.',
            height=dp(92),
            font_size='15sp',
            color=(0.2, 0.2, 0.2, 1),
        )
        self.add_widget(self.merge_status)

        self.merge_progress = ProgressBar(
            max=100,
            value=0,
            size_hint_y=None,
            height=dp(18),
        )
        self.add_widget(self.merge_progress)

        merge_size_row = BoxLayout(
            orientation='horizontal',
            size_hint_y=None,
            height=dp(58),
            spacing=dp(8),
        )
        merge_size_row.add_widget(self._make_label(
            'Seitengrösse:',
            font_size='16sp',
            size_hint_x=0.42,
        ))
        self.merge_page_size = Spinner(
            text='Original',
            values=('Original', 'A3', 'A4', 'A5'),
            option_cls=WhiteSpinnerOption,
            size_hint_x=0.58,
            font_size='18sp',
            background_normal='',
            background_down='',
            background_color=(1, 1, 1, 1),
            color=(0.08, 0.08, 0.08, 1),
            sync_height=True,
        )
        merge_size_row.add_widget(self.merge_page_size)
        self.add_widget(merge_size_row)

        self.add_widget(BoxLayout(size_hint_y=None, height=dp(72)))

        self._apply_compact_layout()

    def _make_button(self, text, height=60, font_size='18sp', background_color=(0.95, 0.95, 0.95, 1)):
        button = Button(
            text=text,
            size_hint_y=None,
            height=height,
            font_size=font_size,
            background_normal='',
            background_down='',
            background_color=background_color,
            color=(0.08, 0.08, 0.08, 1),
            halign='center',
            valign='middle',
        )
        button.bind(size=lambda instance, value: setattr(instance, 'text_size', (instance.width - dp(16), None)))
        return button

    def _make_label(self, text, height=None, font_size='16sp', color=(0.2, 0.2, 0.2, 1), bold=False,
                    halign='left', size_hint_x=1):
        kwargs = {
            'text': text,
            'font_size': font_size,
            'color': color,
            'bold': bold,
            'halign': halign,
            'valign': 'middle',
            'size_hint_x': size_hint_x,
        }
        if height is not None:
            kwargs.update({'size_hint_y': None, 'height': height})
        label = Label(**kwargs)
        label.bind(size=lambda instance, value: setattr(instance, 'text_size', (instance.width, None)))
        return label

    def _apply_compact_layout(self):
        """Use one-column controls on phone-width displays such as Oppo A52."""
        if Window.width > dp(430):
            return

        self.input_row.orientation = 'vertical'
        self.input_row.height = dp(176)
        self.max_size_label.size_hint_x = 1
        self.max_size_label.halign = 'left'
        self.max_size_input.size_hint_x = 1
        self.target_label.size_hint_x = 1
        self.target_label.halign = 'left'
        self.target_input.size_hint_x = 1

    def _set_max_size_preset(self, value):
        self.max_size_input.text = value
        self.target_input.text = ''
        self.status.text = f'Zielgrösse gesetzt: höchstens {value} MB.'

    def show_privacy_policy(self, *args):
        message = (
            'Offline PDF verarbeitet PDF-Dateien lokal auf diesem Gerät.\n\n'
            'Die App hat keine Internetberechtigung, nutzt keine Werbung, '
            'keine Analyse- oder Trackingdienste und sendet keine Dateien an '
            'Server oder Dritte.\n\n'
            'PDF-Zugriff erfolgt nur über den Android-Dateiauswahldialog. '
            'Ausgabedateien bleiben an dem von dir gewählten Speicherort. '
            'Temporäre Dateien und ein kleiner App-Cache können im privaten '
            'App-Speicher liegen und werden nicht in Android-Backups oder '
            'Geräteübertragungen aufgenommen. Du kannst lokale App-Daten über '
            'die Android-App-Info löschen oder die App deinstallieren.\n\n'
            'Kontakt: louisdimitri.hofer@gmail.com\n'
            'Quellcode: https://github.com/louis.hofer/pdfmergercompressor'
        )
        return self._show_wrapped_popup(
            'Datenschutz',
            message,
            size_hint=(0.92, 0.62),
        )

    def _set_compression_progress(self, fraction, message):
        """Update compression progress safely from a worker thread."""
        percent = max(0, min(100, int(fraction * 100)))

        def update(dt):
            self.compression_progress.value = percent
            self.status.text = f'{message} ({percent} %)'

        Clock.schedule_once(update, 0)

    def _set_merge_progress(self, percent, message):
        """Update merge progress safely from a worker thread."""
        percent = max(0, min(100, int(percent)))

        def update(dt):
            self.merge_progress.value = percent
            self.status.text = f'{message} ({percent} %)'

        Clock.schedule_once(update, 0)

    def _get_android_content_resolver(self):
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        return PythonActivity.mActivity.getContentResolver()

    def _acquire_compression_wake_lock(self):
        """Keep the CPU running while Android backgrounds the activity."""
        if platform != 'android' or self.compression_wake_lock is not None:
            return

        try:
            Context = autoclass('android.content.Context')
            PowerManager = autoclass('android.os.PowerManager')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            power_manager = PythonActivity.mActivity.getSystemService(
                Context.POWER_SERVICE
            )
            wake_lock = power_manager.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                'PDFCompressor:Compression',
            )
            wake_lock.setReferenceCounted(False)
            wake_lock.acquire()
            self.compression_wake_lock = wake_lock
            print('[INFO] Android compression wake lock acquired')
        except Exception as e:
            self.compression_wake_lock = None
            print(f'[WARNING] Could not acquire compression wake lock: {e}')

    def _release_compression_wake_lock(self):
        wake_lock = self.compression_wake_lock
        self.compression_wake_lock = None
        if wake_lock is None:
            return

        try:
            if wake_lock.isHeld():
                wake_lock.release()
            print('[INFO] Android compression wake lock released')
        except Exception as e:
            print(f'[WARNING] Could not release compression wake lock: {e}')

    def _open_android_pdf_picker(self, multiple=False):
        """Open Android's Storage Access Framework PDF picker."""
        try:
            Intent = autoclass('android.content.Intent')
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType('application/pdf')
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            if multiple:
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, True)

            request_code = ANDROID_PICK_MERGE_PDF_REQUEST if multiple else ANDROID_PICK_PDF_REQUEST
            try:
                activity.startActivityForResult(intent, request_code)
            except AttributeError:
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                PythonActivity.mActivity.startActivityForResult(intent, request_code)
            return True
        except Exception as e:
            print(f"DEBUG: Android SAF picker failed: {e}")
            return False

    def _open_android_output_tree_picker(self):
        """Open Android's folder picker for a writable output location."""
        try:
            Intent = autoclass('android.content.Intent')
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            intent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            intent.addFlags(Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
            try:
                activity.startActivityForResult(
                    intent,
                    ANDROID_PICK_OUTPUT_DIR_REQUEST,
                )
            except AttributeError:
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                PythonActivity.mActivity.startActivityForResult(
                    intent,
                    ANDROID_PICK_OUTPUT_DIR_REQUEST,
                )
            return True
        except Exception as e:
            print(f"DEBUG: Android output folder picker failed: {e}")
            return False

    def _copy_android_uri_to_temp(self, uri):
        """Copy a content:// PDF URI into an app-private temp file."""
        input_stream = None
        temp_file_path = None
        try:
            resolver = self._get_android_content_resolver()
            input_stream = resolver.openInputStream(uri)
            if input_stream is None:
                raise ValueError('Android returned no input stream')

            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
                temp_file_path = tmp.name
                copy_java_input_stream(input_stream, tmp)
            return temp_file_path
        except Exception:
            safe_unlink(temp_file_path)
            raise
        finally:
            if input_stream is not None:
                try:
                    input_stream.close()
                except Exception as e:
                    print(f"DEBUG: Could not close Android input stream: {e}")

    def _get_android_document_info(self, uri):
        """Return display name and shared-storage folder for a document URI."""
        display_name = None
        relative_dir = None
        resolver = self._get_android_content_resolver()
        cursor = None
        try:
            OpenableColumns = autoclass('android.provider.OpenableColumns')
            cursor = resolver.query(
                uri,
                [OpenableColumns.DISPLAY_NAME],
                None,
                None,
                None,
            )
            if cursor is not None and cursor.moveToFirst():
                name_index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if name_index >= 0:
                    display_name = str(cursor.getString(name_index))
        except Exception as e:
            print(f"DEBUG: Could not read Android document name: {e}")
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as e:
                    print(f"DEBUG: Could not close Android metadata cursor: {e}")

        try:
            DocumentsContract = autoclass('android.provider.DocumentsContract')
            document_id = str(DocumentsContract.getDocumentId(uri))
            if ':' in document_id:
                volume, relative_path = document_id.split(':', 1)
                if volume.lower() == 'primary':
                    parent = os.path.dirname(relative_path).strip('/')
                    relative_dir = f'{parent}/' if parent else ''
        except Exception as e:
            print(f"DEBUG: Could not resolve Android document folder: {e}")

        if not relative_dir:
            cursor = None
            try:
                MediaColumns = autoclass(
                    'android.provider.MediaStore$MediaColumns'
                )
                cursor = resolver.query(
                    uri,
                    [MediaColumns.RELATIVE_PATH],
                    None,
                    None,
                    None,
                )
                if cursor is not None and cursor.moveToFirst():
                    path_index = cursor.getColumnIndex(
                        MediaColumns.RELATIVE_PATH
                    )
                    if path_index >= 0:
                        queried_path = cursor.getString(path_index)
                        if queried_path:
                            relative_dir = str(queried_path).strip('/') + '/'
            except Exception as e:
                print(f"DEBUG: Could not query Android relative path: {e}")
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception as e:
                        print(
                            "DEBUG: Could not close Android path cursor: "
                            f"{e}"
                        )

        if not relative_dir:
            try:
                authority = str(uri.getAuthority() or '').lower()
                if 'downloads' in authority:
                    relative_dir = 'Download/'
            except Exception as e:
                print(f"DEBUG: Could not inspect Android URI authority: {e}")

        return display_name, relative_dir

    def _normalise_android_relative_dir(self, relative_dir):
        if not relative_dir:
            return None
        cleaned = str(relative_dir).strip().strip('/')
        return f'{cleaned}/' if cleaned else None

    def _copy_file_to_java_output_stream(self, source_path, output_stream):
        with open(source_path, 'rb') as source_file:
            for chunk in iter(lambda: source_file.read(256 * 1024), b''):
                output_stream.write(bytearray(chunk))
        output_stream.flush()

    def _publish_android_output_to_tree(self, source_path, output_name):
        """Write a result into the SAF output folder selected by the user."""
        if not self.android_output_tree_uri:
            return None

        resolver = self._get_android_content_resolver()
        output_stream = None
        try:
            Uri = autoclass('android.net.Uri')
            DocumentsContract = autoclass('android.provider.DocumentsContract')
            tree_uri = Uri.parse(self.android_output_tree_uri)
            tree_doc_id = DocumentsContract.getTreeDocumentId(tree_uri)
            parent_uri = DocumentsContract.buildDocumentUriUsingTree(
                tree_uri,
                tree_doc_id,
            )
            output_uri = DocumentsContract.createDocument(
                resolver,
                parent_uri,
                'application/pdf',
                output_name,
            )
            if output_uri is None:
                raise OSError('Android konnte die Ausgabedatei nicht anlegen')

            output_stream = resolver.openOutputStream(output_uri, 'w')
            if output_stream is None:
                raise OSError('Android konnte die Ausgabedatei nicht öffnen')

            self._copy_file_to_java_output_stream(source_path, output_stream)
            output_stream.close()
            output_stream = None
            return (
                f'{self.android_output_tree_label}/{output_name}'
                if self.android_output_tree_label
                else output_name
            )
        except Exception as e:
            print(f"DEBUG: Could not publish into selected Android folder: {e}")
            return None
        finally:
            if output_stream is not None:
                try:
                    output_stream.close()
                except Exception as close_error:
                    print(
                        "DEBUG: Could not close Android tree output stream: "
                        f"{close_error}"
                    )

    def _publish_android_output(self, source_path, output_name, relative_dir=None):
        """Publish a private result into the original shared-storage folder."""
        destination_relative_dir = self._normalise_android_relative_dir(
            relative_dir
        )
        if destination_relative_dir is None:
            destination_relative_dir = self._normalise_android_relative_dir(
                self.android_source_relative_dir
            )
        if destination_relative_dir is None:
            destination_relative_dir = 'Download/'

        resolver = self._get_android_content_resolver()
        output_stream = None
        output_uri = None
        try:
            BuildVersion = autoclass('android.os.Build$VERSION')
            if BuildVersion.SDK_INT < 29:
                return None

            ContentValues = autoclass('android.content.ContentValues')
            MediaStoreDownloads = autoclass(
                'android.provider.MediaStore$Downloads'
            )
            MediaStoreFiles = autoclass('android.provider.MediaStore$Files')
            MediaColumns = autoclass('android.provider.MediaStore$MediaColumns')
            values = ContentValues()
            values.put(MediaColumns.DISPLAY_NAME, output_name)
            values.put(MediaColumns.MIME_TYPE, 'application/pdf')
            values.put(
                MediaColumns.RELATIVE_PATH,
                destination_relative_dir,
            )

            if destination_relative_dir.lower().startswith('download'):
                collection_uri = MediaStoreDownloads.EXTERNAL_CONTENT_URI
            else:
                collection_uri = MediaStoreFiles.getContentUri('external')

            output_uri = resolver.insert(collection_uri, values)
            if output_uri is None:
                raise OSError('Android konnte keine Ausgabedatei anlegen')

            output_stream = resolver.openOutputStream(output_uri, 'w')
            if output_stream is None:
                raise OSError('Android konnte die Ausgabedatei nicht öffnen')

            self._copy_file_to_java_output_stream(source_path, output_stream)
            output_stream.close()
            output_stream = None

            return (
                f'{destination_relative_dir}{output_name}'
                if destination_relative_dir
                else output_name
            )
        except Exception as e:
            print(f"DEBUG: Could not publish beside original PDF: {e}")
            if output_uri is not None:
                try:
                    resolver.delete(output_uri, None, None)
                except Exception as cleanup_error:
                    print(
                        "DEBUG: Could not remove incomplete Android output: "
                        f"{cleanup_error}"
                    )
            return None
        finally:
            if output_stream is not None:
                try:
                    output_stream.close()
                except Exception as close_error:
                    print(
                        "DEBUG: Could not close Android output stream: "
                        f"{close_error}"
                    )

    def _show_wrapped_popup(self, title, message, size_hint=(0.92, 0.52)):
        """Show long messages without text escaping the popup bounds."""
        content = BoxLayout(
            orientation='vertical',
            padding=dp(12),
            spacing=dp(8),
        )
        message_label = Label(
            text=message,
            font_size='16sp',
            size_hint_y=None,
            halign='left',
            valign='top',
        )
        message_label.bind(
            width=lambda instance, value: setattr(
                instance,
                'text_size',
                (value, None),
            ),
            texture_size=lambda instance, value: setattr(
                instance,
                'height',
                value[1] + dp(16),
            ),
        )
        scroll = ScrollView(do_scroll_x=False)
        scroll.add_widget(message_label)
        content.add_widget(scroll)

        popup = Popup(
            title=title,
            content=content,
            size_hint=size_hint,
        )
        popup.open()
        return popup

    def _show_completion_notice(
        self,
        title,
        message,
        location=None,
        timeout=4.5,
    ):
        """Show a stable success notice after long-running work."""
        details = f'{message}\n\n{location}' if location else message
        if platform == 'android':
            print(f"[INFO] {title}: {details}")
            return None

        popup = self._show_wrapped_popup(
            title,
            details,
            size_hint=(0.9, 0.46 if location else 0.34),
        )
        if timeout:
            Clock.schedule_once(lambda dt: popup.dismiss(), timeout)
        return popup

    def _default_output_base_dir(self, source_path):
        if self.output_dir:
            return self.output_dir

        if platform == 'android' and (
            self.android_output_tree_uri
            or (
                source_path
                and (
                    source_path == self.temp_file_path
                    or source_path in self.merge_temp_files
                )
            )
        ):
            try:
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                Environment = autoclass('android.os.Environment')
                output_dir = PythonActivity.mActivity.getExternalFilesDir(
                    Environment.DIRECTORY_DOCUMENTS
                )
                if output_dir is not None:
                    output_path = output_dir.getAbsolutePath()
                    os.makedirs(output_path, exist_ok=True)
                    return output_path
            except Exception as e:
                print(f"DEBUG: Could not resolve app-specific output directory: {e}")
            return os.path.dirname(source_path)

        return os.path.dirname(source_path)

    def _describe_android_tree_uri(self, uri):
        try:
            DocumentsContract = autoclass('android.provider.DocumentsContract')
            document_id = str(DocumentsContract.getTreeDocumentId(uri))
            if ':' in document_id:
                volume, relative_path = document_id.split(':', 1)
                relative_path = relative_path.strip('/')
                if volume.lower() == 'primary':
                    return relative_path or 'Interner Speicher'
                return f'{volume}:{relative_path}' if relative_path else volume
        except Exception as e:
            print(f"DEBUG: Could not describe Android output folder: {e}")
        return 'Ausgewählter Ordner'

    def _on_android_activity_result(self, request_code, result_code, intent):
        if request_code not in (
            ANDROID_PICK_PDF_REQUEST,
            ANDROID_PICK_MERGE_PDF_REQUEST,
            ANDROID_PICK_OUTPUT_DIR_REQUEST,
        ):
            return

        Activity = autoclass('android.app.Activity')
        if result_code != Activity.RESULT_OK or intent is None:
            print("DEBUG: Android picker cancelled")
            return

        try:
            if request_code == ANDROID_PICK_OUTPUT_DIR_REQUEST:
                uri = intent.getData()
                if uri is None:
                    self.status.text = 'Kein Ausgabeordner ausgewählt.'
                    return
                Intent = autoclass('android.content.Intent')
                flags = intent.getFlags()
                persist_flags = flags & (
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                )
                try:
                    self._get_android_content_resolver().takePersistableUriPermission(
                        uri,
                        persist_flags,
                    )
                except Exception as e:
                    print(f"DEBUG: Could not persist Android tree permission: {e}")
                self.android_output_tree_uri = uri.toString()
                self.android_output_tree_label = self._describe_android_tree_uri(uri)
                self.output_dir = None
                self.output_dir_label.text = (
                    f'Ausgabe: {self.android_output_tree_label}'
                )
                self.status.text = (
                    f'Speicherordner: {self.android_output_tree_label}'
                )
                return

            uris = []
            clip_data = intent.getClipData()
            if clip_data:
                for index in range(clip_data.getItemCount()):
                    uris.append(clip_data.getItemAt(index).getUri())

            data_uri = intent.getData()
            if data_uri:
                uris.append(data_uri)

            unique_uris = []
            seen = set()
            for uri in uris:
                uri_text = uri.toString()
                if uri_text not in seen:
                    seen.add(uri_text)
                    unique_uris.append(uri)

            if not unique_uris:
                self.status.text = 'Keine PDF ausgewählt.'
                return

            for uri in unique_uris:
                display_name, relative_dir = self._get_android_document_info(uri)
                temp_path = self._copy_android_uri_to_temp(uri)
                if not is_valid_pdf_path(temp_path):
                    safe_unlink(temp_path)
                    Popup(
                        title='Hinweis',
                        content=Label(text='Die ausgewählte Datei ist keine gültige PDF.', font_size='16sp'),
                        size_hint=(0.85, 0.35),
                    ).open()
                    continue

                if request_code == ANDROID_PICK_MERGE_PDF_REQUEST:
                    self._add_pdf_to_merge_list(
                        temp_path,
                        display_name=display_name,
                        relative_dir=relative_dir,
                    )
                    self.merge_temp_files.append(temp_path)
                else:
                    if self.temp_file_path:
                        safe_unlink(self.temp_file_path)
                    self.input_path = temp_path
                    self.temp_file_path = temp_path
                    self.android_source_name = display_name
                    self.android_source_relative_dir = relative_dir
                    file_size = format_size(os.path.getsize(temp_path))
                    shown_name = display_name or 'PDF aus Android-Dateiauswahl'
                    self.status.text = f'Ausgewählt: {shown_name} ({file_size})'
                    self.compress_btn.disabled = False
                    if not self.merge_list:
                        self._reset_merge_page_editor()
                    self._update_merge_status()

            if request_code == ANDROID_PICK_MERGE_PDF_REQUEST:
                self._update_merge_status()
                self.merge_mode = False
        except Exception as e:
            print(f"DEBUG: Android picker result failed: {e}")
            Popup(
                title='Fehler',
                content=Label(text=f'PDF konnte nicht geladen werden:\n{e}', font_size='16sp'),
                size_hint=(0.9, 0.45),
            ).open()

    def _update_rect(self, instance, value):
        self.rect.pos = instance.pos
        self.rect.size = instance.size

    def _resolve_android_base_dir(self):
        """Find a readable shared storage base directory on Android."""
        candidates = []
        try:
            ext_dir = storagepath.get_external_storage_dir()
            if ext_dir:
                candidates.append(ext_dir)
        except Exception as e:
            print(f"DEBUG: Could not read external storage dir from plyer: {e}")

        if platform == 'android':
            try:
                Environment = autoclass('android.os.Environment')
                java_ext_dir = Environment.getExternalStorageDirectory().getAbsolutePath()
                if java_ext_dir:
                    candidates.append(java_ext_dir)
            except Exception as e:
                print(f"DEBUG: Could not read external storage dir from Android API: {e}")

        candidates.extend(['/storage/emulated/0', '/sdcard'])

        # Pick the first existing, readable directory
        for path in candidates:
            if path and os.path.isdir(path) and os.access(path, os.R_OK):
                return path

        # Last-resort fallback
        return os.path.expanduser('~')

    def _find_initial_pdf_dir(self, base_dir):
        """Choose a sensible initial folder likely containing PDFs."""
        preferred_dirs = [
            os.path.join(base_dir, 'Download'),
            os.path.join(base_dir, 'Downloads'),
            os.path.join(base_dir, 'Documents'),
            base_dir,
        ]

        print(f"DEBUG: Searching for PDFs in preferred directories...")
        for directory in preferred_dirs:
            if not os.path.isdir(directory):
                print(f"DEBUG: Skipping non-existent directory: {directory}")
                continue
            try:
                items = os.listdir(directory)
                print(f"DEBUG: Checking {directory} - found {len(items)} items")
                pdf_count = 0
                for item in items:
                    # Don't use os.path.isfile() - fails on OPPO/scoped storage
                    # Just check extension for any non-hidden file
                    if not item.startswith('.') and item.lower().endswith('.pdf'):
                        pdf_count += 1
                        print(f"DEBUG: Found PDF: {item}")
                        return directory  # Return first directory with PDFs
                print(f"DEBUG: No PDFs in {directory} ({pdf_count} PDF files found)")
            except Exception as e:
                print(f"DEBUG: Error listing {directory}: {e}")
                continue

        print(f"DEBUG: No PDFs found in preferred dirs, using base: {base_dir}")
        return base_dir

    def select_file(self, *args):
        print("DEBUG: select_file called")
        if platform == 'android':
            if self._open_android_pdf_picker(multiple=False):
                print("DEBUG: Android SAF PDF picker opened")
                return
            if filechooser:
                try:
                    filechooser.open_file(on_selection=self._file_chosen)
                    print("DEBUG: Plyer Android file chooser opened")
                    return
                except Exception as e:
                    print(f"DEBUG: Plyer Android file chooser failed: {e}")
            self.show_file_browser()
        else:
            # Fallback to system file chooser on desktop
            if filechooser:
                print("DEBUG: Using plyer filechooser")
                try:
                    filechooser.open_file(on_selection=self._file_chosen)
                    print("DEBUG: filechooser.open_file called")
                    return
                except Exception as e:
                    print(f"DEBUG: filechooser failed: {e}")

            # Fallback: open a simple popup with instructions (desktop users can drag-drop or run differently)
            print("DEBUG: Using fallback popup")
            popup = Popup(title='Datei auswählen', content=Label(text='Dateiauswahl nicht verfügbar.', font_size='16sp'), size_hint=(0.8, 0.4))
            popup.open()

    def show_file_browser(self, start_dir=None):
        """Show hierarchical file browser with folder navigation."""
        print("DEBUG: Showing integrated file browser")
        try:
            if platform == 'android':
                base_dir = self._resolve_android_base_dir()
                print(f"DEBUG: Resolved Android base directory: {base_dir}")
            else:
                base_dir = storagepath.get_external_storage_dir() or os.path.expanduser('~')
                print(f"DEBUG: Resolved base directory: {base_dir}")

            if start_dir is None:
                current_dir = self._find_initial_pdf_dir(base_dir)
                print(f"DEBUG: Initial PDF directory: {current_dir}")
            else:
                current_dir = start_dir
                print(f"DEBUG: Using provided start directory: {current_dir}")

            # Get list of folders and PDF files in current directory
            folders = []
            pdf_files = []

            print(f"DEBUG: Listing contents of: {current_dir}")
            try:
                items = os.listdir(current_dir)
                print(f"DEBUG: Found {len(items)} items in directory")

                for item in sorted(items):
                    # Skip hidden files
                    if item.startswith('.'):
                        continue

                    item_path = os.path.join(current_dir, item)

                    # Try to check if it's a directory first
                    is_dir = False
                    try:
                        is_dir = os.path.isdir(item_path)
                    except Exception as e:
                        print(f"DEBUG: Cannot check if directory {item}: {e}")

                    if is_dir:
                        folders.append(item)
                        print(f"DEBUG: Added folder: {item}")
                    # For files, don't use os.path.isfile() - fails on OPPO/scoped storage
                    # Just check extension for anything that's not a directory
                    elif item.lower().endswith('.pdf'):
                        pdf_files.append(item)
                        print(f"DEBUG: Added PDF file: {item}")
                    else:
                        # Not a directory and not a PDF - skip silently
                        pass

                print(f"DEBUG: Final count - {len(folders)} folders, {len(pdf_files)} PDFs")
            except Exception as e:
                print(f"DEBUG: Error listing directory: {e}")
                popup = Popup(title='Error', content=Label(text=f'Cannot read directory:\n{e}', font_size='16sp'),
                            size_hint=(0.8, 0.4))
                popup.open()
                return

            # Create UI
            content = BoxLayout(orientation='vertical', size_hint_y=None)
            content.bind(minimum_height=content.setter('height'))

            # Show current path
            rel_path = os.path.relpath(current_dir, base_dir) if current_dir != base_dir else '/'
            path_label = Label(text=f'Ordner: {rel_path}', size_hint_y=None, height=dp(44),
                             font_size='14sp', color=(0.3, 0.3, 0.3, 1))

            # Add "Up" button if not at base directory
            if current_dir != base_dir:
                up_btn = Button(text='.. zurück', size_hint_y=None, height=dp(58), font_size='18sp',
                              background_color=(0.93, 0.93, 0.93, 1), color=(0.1, 0.1, 0.1, 1), halign='left')
                up_btn.bind(texture_size=up_btn.setter('text_size'))
                parent_dir = os.path.dirname(current_dir)
                up_btn.bind(on_release=lambda x: self._navigate_to_folder(parent_dir))
                content.add_widget(up_btn)

            # Add folders
            for folder in folders:
                folder_path = os.path.join(current_dir, folder)
                btn = Button(text=f'[Ordner] {folder}', size_hint_y=None, height=dp(58), font_size='16sp',
                           background_color=(0.96, 0.96, 0.96, 1), color=(0.1, 0.1, 0.1, 1), halign='left')
                btn.bind(texture_size=btn.setter('text_size'))
                btn.bind(on_release=lambda instance, path=folder_path: self._navigate_to_folder(path))
                content.add_widget(btn)

            # Add PDF files
            for pdf in pdf_files:
                pdf_path = os.path.join(current_dir, pdf)
                btn = Button(text=f'{pdf}', size_hint_y=None, height=dp(58), font_size='16sp',
                           background_color=(0.97, 0.97, 0.97, 1), color=(0.15, 0.15, 0.15, 1), halign='left')
                btn.bind(texture_size=btn.setter('text_size'))
                btn.bind(on_release=lambda instance, path=pdf_path: self._on_file_selected(path))
                content.add_widget(btn)

            if not folders and not pdf_files:
                content.add_widget(Label(text='(Keine PDFs in diesem Ordner)', size_hint_y=None, height=dp(52),
                                       font_size='16sp', color=(0.5, 0.5, 0.5, 1)))

            # Create scrollable view
            scroll_view = ScrollView(size_hint=(1, 1))
            scroll_view.add_widget(content)

            # Create popup layout
            popup_content = BoxLayout(orientation='vertical')
            popup_content.add_widget(path_label)
            popup_content.add_widget(scroll_view)

            # Add bottom buttons
            btn_box = BoxLayout(size_hint_y=None, height=dp(64), spacing=dp(6))
            close_btn = Button(text='Abbrechen', font_size='18sp',
                             background_color=(0.96, 0.96, 0.96, 1), color=(0.1, 0.1, 0.1, 1))
            close_btn.bind(on_release=lambda x: self.current_popup.dismiss())
            btn_box.add_widget(close_btn)

            if self.merge_mode:
                done_btn = Button(text='Fertig', font_size='18sp',
                                background_color=ACTION_BUTTON_COLOR, color=(0.1, 0.1, 0.1, 1))
                done_btn.bind(on_release=lambda x: self._finish_merge_selection())
                btn_box.add_widget(done_btn)

            popup_content.add_widget(btn_box)

            popup = Popup(title='PDF auswählen', content=popup_content, size_hint=(0.95, 0.95))
            self.current_popup = popup
            self.current_browser_dir = current_dir
            popup.open()

        except Exception as e:
            print(f"DEBUG: Error in file browser: {e}")
            import traceback
            traceback.print_exc()
            popup = Popup(title='Fehler', content=Label(text=f'Dateibrowser konnte nicht geöffnet werden:\n{e}', font_size='16sp'),
                        size_hint=(0.8, 0.4))
            popup.open()

    def _navigate_to_folder(self, folder_path):
        """Navigate to a new folder in the file browser."""
        if hasattr(self, 'current_popup'):
            self.current_popup.dismiss()
        self.show_file_browser(start_dir=folder_path)

    def _on_file_selected(self, file_path):
        """Handle file selection from hierarchical browser."""
        print(f"DEBUG: File selected: {file_path}")

        # Don't use os.path.isdir() on OPPO - may fail under scoped storage
        # The file browser already filters correctly, just check extension
        if not file_path.lower().endswith('.pdf'):
            print("DEBUG: Selected path is not a PDF")
            return

        if not is_valid_pdf_path(file_path):
            popup = Popup(title='Hinweis', content=Label(text='Die ausgewählte Datei ist keine gültige PDF.', font_size='16sp'), size_hint=(0.8, 0.35))
            popup.open()
            return

        print(f"DEBUG: PDF file confirmed: {os.path.basename(file_path)}")

        if self.merge_mode:
            # Add to merge list
            self._add_pdf_to_merge_list(file_path)
            self.merge_btn.disabled = len(self.merge_list) < 2
            # Don't close popup, allow selecting more files
            popup = Popup(title='Hinzugefügt', content=Label(text=f'{os.path.basename(file_path)}\n\nWeitere PDF wählen oder "Fertig" tippen.',
                                                       font_size='16sp'), size_hint=(0.7, 0.35))
            popup.open()
        else:
            # Single file selection for compression
            self.input_path = file_path
            self.temp_file_path = None
            self.android_source_name = None
            self.android_source_relative_dir = None
            try:
                file_size = format_size(os.path.getsize(file_path))
                self.status.text = f'Ausgewählt: {os.path.basename(file_path)} ({file_size})'
            except Exception as e:
                # os.path.getsize() may fail on some Android devices/OPPO under scoped storage
                print(f"DEBUG: Cannot get file size: {e}")
                self.status.text = f'Ausgewählt: {os.path.basename(file_path)}'
            self.compress_btn.disabled = False
            if not self.merge_list:
                self._reset_merge_page_editor()
            self._update_merge_status()
            if hasattr(self, 'current_popup'):
                self.current_popup.dismiss()

    def _finish_merge_selection(self):
        """Finish selecting files for merge."""
        self.merge_mode = False
        if hasattr(self, 'current_popup'):
            self.current_popup.dismiss()
        if len(self.merge_list) > 0:
            popup = Popup(title='Bereit',
                        content=Label(text=f'{len(self.merge_list)} PDFs in der Merge-Liste.\n\nJetzt "Merge starten" tippen.',
                                    font_size='16sp'),
                        size_hint=(0.8, 0.4))
            popup.open()

    def _add_pdf_to_merge_list(self, path, display_name=None, relative_dir=None):
        if path not in self.merge_list:
            self.merge_list.append(path)
            self.merge_display_names[path] = (
                display_name or os.path.basename(path)
            )
            if relative_dir:
                self.merge_source_relative_dirs[path] = relative_dir
            self._reset_merge_page_editor()
        elif display_name:
            self.merge_display_names[path] = display_name
        self._update_merge_status()

    def _merge_display_name(self, path):
        return self.merge_display_names.get(path) or os.path.basename(path)

    def _reset_merge_page_editor(self):
        self.merge_preview_generation += 1
        self.merge_page_order = []
        self.merge_page_order_dirty = False
        if self.merge_preview_dir:
            shutil.rmtree(self.merge_preview_dir, ignore_errors=True)
            self.merge_preview_dir = None

    def _cleanup_merge_temp_files(self):
        for path in self.merge_temp_files:
            safe_unlink(path)
        self.merge_temp_files = []

    def _update_merge_status(self):
        count = len(self.merge_list)
        if count == 0:
            if self.input_path and is_valid_pdf_path(self.input_path):
                shown_name = self.android_source_name or os.path.basename(
                    self.input_path
                )
                self.merge_status.text = (
                    f'1 PDF ausgewählt:\n{shown_name}\n'
                    'Seiten bearbeiten möglich.'
                )
                self.merge_btn.disabled = True
                self.edit_merge_btn.disabled = False
                return
            self.merge_status.text = 'Merge-Liste leer. Mindestens 2 PDFs hinzufügen.'
            self.merge_btn.disabled = True
            self.edit_merge_btn.disabled = True
            return

        names = [
            self._merge_display_name(path)
            for path in self.merge_list[:3]
        ]
        more = f'\n+ {count - 3} weitere' if count > 3 else ''
        edit_note = (
            f'\nBearbeitet: {len(self.merge_page_order)} Seiten'
            if self.merge_page_order_dirty and self.merge_page_order
            else ''
        )
        self.merge_status.text = (
            f'{count} PDF{"s" if count != 1 else ""} ausgewählt:\n'
            + '\n'.join(names)
            + more
            + edit_note
        )
        self.merge_btn.disabled = not self._can_start_merge()
        self.edit_merge_btn.disabled = False

    def _can_start_merge(self):
        if len(self.merge_list) >= 2:
            return True
        return bool(self.merge_page_order_dirty and self.merge_page_order)

    def _prepare_single_pdf_for_page_editor(self):
        if self.merge_list:
            return True
        if not self.input_path or not is_valid_pdf_path(self.input_path):
            return False
        self._add_pdf_to_merge_list(
            self.input_path,
            display_name=self.android_source_name or os.path.basename(
                self.input_path
            ),
            relative_dir=self.android_source_relative_dir,
        )
        return True

    def open_merge_page_editor(self, *args):
        """Show page thumbnails and allow reorder/removal before merging."""
        if not self._prepare_single_pdf_for_page_editor():
            self._show_wrapped_popup(
                'Hinweis',
                'Bitte zuerst eine PDF auswählen oder PDFs zur Merge-Liste '
                'hinzufügen.',
                size_hint=(0.88, 0.38),
            )
            return

        root = BoxLayout(
            orientation='vertical',
            spacing=dp(8),
            padding=dp(10),
        )
        editor_status = self._make_label(
            'Seitenvorschau wird erstellt...',
            height=dp(48),
            font_size='16sp',
        )
        root.add_widget(editor_status)
        page_scroll = ScrollView(
            do_scroll_x=False,
            scroll_type=['bars', 'content'],
            bar_width=dp(6),
        )
        page_list = BoxLayout(
            orientation='vertical',
            size_hint_y=None,
            spacing=dp(8),
        )
        page_list.bind(minimum_height=page_list.setter('height'))
        page_scroll.add_widget(page_list)
        root.add_widget(page_scroll)
        close_btn = self._make_button(
            'Änderungen übernehmen',
            height=dp(58),
            font_size='18sp',
        )
        root.add_widget(close_btn)

        popup = Popup(
            title='Merge-Seiten bearbeiten',
            content=root,
            size_hint=(0.96, 0.94),
            auto_dismiss=False,
        )
        close_btn.bind(on_release=popup.dismiss)
        self.merge_editor_popup = popup
        popup.open()

        if self.merge_page_order:
            editor_status.text = f'{len(self.merge_page_order)} Seiten'
            self._render_merge_page_rows(page_list, editor_status)
            return

        self._reset_merge_page_editor()
        self.merge_preview_dir = tempfile.mkdtemp(prefix='pdf-merge-preview-')
        preview_dir = self.merge_preview_dir
        preview_generation = self.merge_preview_generation
        merge_paths = list(self.merge_list)

        def task():
            entries = []
            try:
                def add_preview_entry(
                    file_index,
                    path,
                    display_name,
                    page_index,
                    thumbnail,
                ):
                    entries.append({
                        'id': f'{file_index}:{page_index}',
                        'path': path,
                        'page': page_index,
                        'thumbnail': thumbnail,
                        'name': display_name,
                    })
                    loaded = len(entries)
                    snapshot = list(entries)

                    def on_partial(dt):
                        if (
                            self.merge_editor_popup is not popup
                            or self.merge_preview_generation != preview_generation
                            or self.merge_page_order_dirty
                        ):
                            return
                        editor_status.text = (
                            f'Vorschau wird erstellt... {loaded} Seiten geladen'
                        )
                        previous_scroll_y = page_scroll.scroll_y
                        self.merge_page_order = snapshot
                        self._render_merge_page_rows(
                            page_list,
                            editor_status,
                        )
                        page_scroll.scroll_y = previous_scroll_y

                    Clock.schedule_once(on_partial, 0)

                for file_index, path in enumerate(merge_paths):
                    display_name = self._merge_display_name(path)
                    pattern = os.path.join(
                        preview_dir,
                        f'doc-{file_index:03d}-page-%05d.png',
                    )
                    before_count = len(entries)

                    def on_thumbnail(
                        page_index,
                        page_count,
                        thumbnail,
                        current_file_index=file_index,
                        current_path=path,
                        current_display_name=display_name,
                    ):
                        add_preview_entry(
                            current_file_index,
                            current_path,
                            current_display_name,
                            page_index,
                            thumbnail,
                        )

                    thumbnails = render_pdf_thumbnails(
                        path,
                        pattern,
                        progress_callback=on_thumbnail,
                    )
                    if len(entries) == before_count:
                        for page_index, thumbnail in enumerate(thumbnails, start=1):
                            add_preview_entry(
                                file_index,
                                path,
                                display_name,
                                page_index,
                                thumbnail,
                            )
                if not entries:
                    raise RuntimeError('In den PDFs wurden keine Seiten gefunden.')

                def on_ready(dt):
                    if (
                        self.merge_editor_popup is not popup
                        or self.merge_preview_generation != preview_generation
                    ):
                        return
                    previous_scroll_y = page_scroll.scroll_y
                    self.merge_page_order = entries
                    editor_status.text = f'{len(entries)} Seiten'
                    self._render_merge_page_rows(page_list, editor_status)
                    page_scroll.scroll_y = previous_scroll_y

                Clock.schedule_once(on_ready, 0)
            except Exception as e:
                error_message = str(e)

                def on_error(dt):
                    if (
                        self.merge_editor_popup is not popup
                        or self.merge_preview_generation != preview_generation
                    ):
                        return
                    editor_status.text = f'Vorschau fehlgeschlagen: {error_message}'

                Clock.schedule_once(on_error, 0)

        threading.Thread(target=task, daemon=True).start()

    def _merge_page_id(self, entry, fallback_index):
        return entry.get(
            'id',
            f'{entry.get("path", "")}:{entry.get("page", "")}:{fallback_index}',
        )

    def _find_merge_page_index(self, page_id):
        for index, entry in enumerate(self.merge_page_order):
            if self._merge_page_id(entry, index) == page_id:
                return index
        return -1

    def _render_merge_page_rows(self, page_list, editor_status):
        page_list.clear_widgets()
        for index, entry in enumerate(self.merge_page_order):
            page_id = self._merge_page_id(entry, index)
            row = BoxLayout(
                orientation='horizontal',
                size_hint_y=None,
                height=dp(196),
                spacing=dp(8),
                padding=dp(5),
            )
            preview = Image(
                source=entry['thumbnail'],
                size_hint_x=0.44,
                fit_mode='contain',
            )
            row.add_widget(preview)
            row.add_widget(self._make_label(
                f'{index + 1}\n{entry["name"]}\nSeite {entry["page"]}',
                font_size='14sp',
                size_hint_x=0.30,
            ))
            controls = BoxLayout(
                orientation='vertical',
                size_hint_x=0.26,
                spacing=dp(5),
            )
            up_btn = self._make_button('Nach oben', height=dp(54), font_size='13sp')
            up_btn.disabled = index == 0
            up_btn.bind(
                on_release=lambda instance, item_id=page_id:
                self._move_merge_page(item_id, -1, page_list, editor_status)
            )
            controls.add_widget(up_btn)
            down_btn = self._make_button('Nach unten', height=dp(54), font_size='13sp')
            down_btn.disabled = index == len(self.merge_page_order) - 1
            down_btn.bind(
                on_release=lambda instance, item_id=page_id:
                self._move_merge_page(item_id, 1, page_list, editor_status)
            )
            controls.add_widget(down_btn)
            remove_btn = self._make_button(
                'Entfernen',
                height=dp(54),
                font_size='13sp',
                background_color=(0.96, 0.86, 0.86, 1),
            )
            remove_btn.bind(
                on_release=lambda instance, item_id=page_id:
                self._remove_merge_page(item_id, page_list, editor_status)
            )
            controls.add_widget(remove_btn)
            row.add_widget(controls)
            page_list.add_widget(row)

    def _move_merge_page(self, page_id, offset, page_list, editor_status):
        old_index = self._find_merge_page_index(page_id)
        if not 0 <= old_index < len(self.merge_page_order):
            return
        new_index = old_index + offset
        if not 0 <= new_index < len(self.merge_page_order):
            return
        self.merge_page_order.insert(new_index, self.merge_page_order.pop(old_index))
        self.merge_page_order_dirty = True
        editor_status.text = f'Reihenfolge geändert: {len(self.merge_page_order)} Seiten'
        self._update_merge_status()
        self._render_merge_page_rows(page_list, editor_status)

    def _remove_merge_page(self, page_id, page_list, editor_status):
        if len(self.merge_page_order) <= 1:
            editor_status.text = 'Mindestens eine Seite muss übrig bleiben.'
            return
        index = self._find_merge_page_index(page_id)
        if not 0 <= index < len(self.merge_page_order):
            return
        self.merge_page_order.pop(index)
        self.merge_page_order_dirty = True
        editor_status.text = f'Gelöscht: {len(self.merge_page_order)} Seiten übrig'
        self._update_merge_status()
        self._render_merge_page_rows(page_list, editor_status)

    def _file_chosen(self, selection):
        print(f"DEBUG: _file_chosen called with selection: {selection}")
        if not selection:
            print("DEBUG: No selection made")
            return
        path = selection[0]
        print(f"DEBUG: Selected path: {path}")

        temp_file_path = None
        android_source_name = None
        android_source_relative_dir = None
        if platform == 'android' and path.startswith('content://'):
            print("DEBUG: Handling Android content URI")
            try:
                uri = autoclass('android.net.Uri').parse(path)
                (
                    android_source_name,
                    android_source_relative_dir,
                ) = self._get_android_document_info(uri)
                temp_file_path = self._copy_android_uri_to_temp(uri)
                path = temp_file_path
                print(f"DEBUG: Copied to temp file: {path}")
            except Exception as e:
                print(f"DEBUG: Error handling URI: {e}")
                popup = Popup(title='Error', content=Label(text=f'Failed to access file:\n{e}', font_size='16sp'), size_hint=(0.8, 0.4))
                popup.open()
                return

        if is_valid_pdf_path(path):
            print("DEBUG: Valid PDF file selected")
            if self.merge_mode:
                self._add_pdf_to_merge_list(
                    path,
                    display_name=android_source_name,
                    relative_dir=android_source_relative_dir,
                )
                if temp_file_path:
                    self.merge_temp_files.append(temp_file_path)
                self.merge_mode = False
            else:
                if self.temp_file_path and self.temp_file_path != temp_file_path:
                    safe_unlink(self.temp_file_path)
                self.input_path = path
                self.temp_file_path = temp_file_path
                self.android_source_name = android_source_name
                self.android_source_relative_dir = android_source_relative_dir
                file_size = format_size(os.path.getsize(path))
                shown_name = android_source_name or os.path.basename(path)
                self.status.text = f'Ausgewählt: {shown_name} ({file_size})'
                self.compress_btn.disabled = False
                if not self.merge_list:
                    self._reset_merge_page_editor()
                self._update_merge_status()
        else:
            print("DEBUG: Selected file is not a valid PDF")
            if temp_file_path:
                safe_unlink(temp_file_path)  # Clean up temp file if invalid

    def _file_chosen_multiple(self, selection):
        print(f"DEBUG: _file_chosen_multiple called with selection: {selection}")
        if not selection:
            print("DEBUG: No selection made")
            return
        for path in selection:
            temp_file_path = None
            display_name = None
            relative_dir = None
            if platform == 'android' and path.startswith('content://'):
                try:
                    uri = autoclass('android.net.Uri').parse(path)
                    display_name, relative_dir = self._get_android_document_info(uri)
                    temp_file_path = self._copy_android_uri_to_temp(uri)
                    path = temp_file_path
                except Exception as e:
                    print(f"DEBUG: Error handling URI: {e}")
                    continue
            if is_valid_pdf_path(path):
                self._add_pdf_to_merge_list(
                    path,
                    display_name=display_name,
                    relative_dir=relative_dir,
                )
                if temp_file_path:
                    self.merge_temp_files.append(temp_file_path)
            else:
                if temp_file_path:
                    safe_unlink(temp_file_path)
        self._update_merge_status()
        self.merge_mode = False

    def compress(self, *args):
        print("DEBUG: compress called")
        if not self.input_path:
            print("DEBUG: No input path set")
            popup = Popup(title='Hinweis', content=Label(text='Bitte zuerst eine PDF auswählen.', font_size='16sp'), size_hint=(0.8, 0.3))
            popup.open()
            return

        if not is_valid_pdf_path(self.input_path):
            popup = Popup(title='Hinweis', content=Label(text='Die ausgewählte Datei ist keine gültige oder lesbare PDF.', font_size='16sp'), size_hint=(0.85, 0.35))
            popup.open()
            self.compress_btn.disabled = True
            return

        # Use custom output directory if set, otherwise use same as input
        output_base_dir = self._default_output_base_dir(self.input_path)
        if not os.path.isdir(output_base_dir):
            popup = Popup(title='Hinweis', content=Label(text='Der Speicherordner ist nicht verfügbar.', font_size='16sp'), size_hint=(0.8, 0.3))
            popup.open()
            return
        input_name = self.android_source_name or os.path.basename(self.input_path)
        output_name = f'{os.path.splitext(input_name)[0]}_compressed.pdf'
        out_path = os.path.join(output_base_dir, output_name)
        # Ensure unique filename
        counter = 1
        original_out_path = out_path
        while os.path.exists(out_path):
            name, ext = os.path.splitext(original_out_path)
            out_path = os.path.join(os.path.dirname(original_out_path), f"{name}_{counter}{ext}")
            counter += 1  # Create _compressed.pdf
        print(f"DEBUG: Output path: {out_path}")
        self.status.text = 'Komprimierung wird vorbereitet...'
        self.compression_progress.value = 0
        self.compress_btn.disabled = True

        # Get max size (MB)
        max_size = None
        if self.max_size_input.text.strip():
            try:
                max_size = float(self.max_size_input.text.strip())
                if max_size <= 0 or max_size > MAX_TARGET_SIZE_MB:
                    raise ValueError()
            except ValueError:
                popup = Popup(
                    title='Hinweis',
                    content=Label(text=f'Die Zielgrösse muss zwischen 0 und {MAX_TARGET_SIZE_MB} MB liegen.', font_size='16sp'),
                    size_hint=(0.8, 0.35),
                )
                popup.open()
                self.compress_btn.disabled = False
                return

        resize = False

        # Get target compression percentage (0-80)
        target_reduction = None
        if self.target_input.text.strip():
            try:
                target_reduction = float(self.target_input.text.strip())
                if target_reduction < 0 or target_reduction > 80:
                    raise ValueError()
            except (ValueError, AttributeError):
                popup = Popup(
                    title='Hinweis',
                    content=Label(text='Die Prozent-Angabe muss zwischen 0 und 80 liegen.', font_size='16sp'),
                    size_hint=(0.8, 0.35),
                )
                popup.open()
                self.compress_btn.disabled = False
                return

        self.compression_running = True
        self._acquire_compression_wake_lock()

        def task():
            try:
                success = compress_pdf(
                    self.input_path,
                    out_path,
                    max_size,
                    resize,
                    dpi=None,
                    target_reduction=target_reduction,
                    progress_callback=self._set_compression_progress,
                )
                if not success or not os.path.exists(out_path):
                    raise RuntimeError('Für diese PDF konnte keine komprimierte Datei erstellt werden.')
                in_size = os.path.getsize(self.input_path)
                out_size = os.path.getsize(out_path)
                reduction = ((in_size - out_size) / in_size) * 100 if in_size else 0
                saved_location = out_path
                if platform == 'android' and not self.output_dir:
                    if self.android_output_tree_uri:
                        published_location = self._publish_android_output_to_tree(
                            out_path,
                            output_name,
                        )
                    elif self.input_path == self.temp_file_path:
                        published_location = self._publish_android_output(
                            out_path,
                            output_name,
                        )
                    else:
                        published_location = None
                    if not published_location:
                        raise RuntimeError(
                            'Die PDF konnte nicht gespeichert werden. Bitte '
                            'einen Speicherordner auswählen.'
                        )
                    saved_location = published_location

                def on_done(dt):
                    in_size_fmt = format_size(in_size)
                    out_size_fmt = format_size(out_size)
                    self.status.text = f'Gespeichert: {output_name} | {in_size_fmt} -> {out_size_fmt} ({reduction:.1f}% kleiner)'
                    self.compression_progress.value = 100

                    self._show_completion_notice(
                        'Gespeichert',
                        (
                            f'{in_size_fmt} -> {out_size_fmt} '
                            f'({reduction:.1f}% kleiner)'
                        ),
                        location=saved_location,
                    )
                    self.compress_btn.disabled = False

                Clock.schedule_once(on_done, 0)
            except Exception as e:
                error_msg = str(e)
                def on_error(dt):
                    self.status.text = 'Komprimierung fehlgeschlagen'
                    self.compression_progress.value = 0
                    self._show_wrapped_popup(
                        'Fehler',
                        f'Komprimierung fehlgeschlagen:\n\n{error_msg}',
                    )
                    self.compress_btn.disabled = False

                Clock.schedule_once(on_error, 0)
            finally:
                self.compression_running = False
                self._release_compression_wake_lock()

        threading.Thread(target=task, daemon=True).start()

    def select_output_directory(self, *args):
        """Allow user to select output directory for saved files."""
        if platform == 'android':
            if self._open_android_output_tree_picker():
                return
            self.show_directory_browser()
        else:
            # Desktop: show a simple input popup
            content = BoxLayout(orientation='vertical', padding=10, spacing=10)
            content.add_widget(Label(text='Ausgabeordner eingeben:', font_size='16sp'))
            dir_input = TextInput(text=self.output_dir or '', multiline=False, font_size='16sp')
            content.add_widget(dir_input)
            btn_box = BoxLayout(size_hint_y=None, height='50dp', spacing=10)
            ok_btn = Button(text='OK', background_color=ACTION_BUTTON_COLOR, color=(0.1, 0.1, 0.1, 1))
            cancel_btn = Button(text='Abbrechen', background_color=(0.96, 0.96, 0.96, 1), color=(0.1, 0.1, 0.1, 1))
            btn_box.add_widget(cancel_btn)
            btn_box.add_widget(ok_btn)
            content.add_widget(btn_box)
            popup = Popup(title='Ausgabeordner wählen', content=content, size_hint=(0.9, 0.5))
            ok_btn.bind(on_release=lambda x: self._set_output_dir(dir_input.text, popup))
            cancel_btn.bind(on_release=lambda x: popup.dismiss())
            popup.open()

    def show_directory_browser(self, start_dir=None):
        """Show hierarchical directory browser for Android."""
        try:
            base_dir = self._resolve_android_base_dir()
            if start_dir is None:
                current_dir = base_dir
            else:
                current_dir = start_dir

            # Get list of folders in current directory
            folders = []
            try:
                items = os.listdir(current_dir)
                for item in sorted(items):
                    item_path = os.path.join(current_dir, item)
                    try:
                        if os.path.isdir(item_path):
                            folders.append(item)
                    except Exception as e:
                        print(f"DEBUG: Error checking item {item}: {e}")
                        continue
            except Exception as e:
                print(f"DEBUG: Error listing directory: {e}")
                popup = Popup(title='Error', content=Label(text=f'Cannot read directory:\n{e}', font_size='16sp'),
                            size_hint=(0.8, 0.4))
                popup.open()
                return

            # Create UI
            content = BoxLayout(orientation='vertical', size_hint_y=None)
            content.bind(minimum_height=content.setter('height'))

            # Show current path
            rel_path = os.path.relpath(current_dir, base_dir) if current_dir != base_dir else '/'
            path_label = Label(text=f'Ordner: {rel_path}', size_hint_y=None, height=dp(44),
                             font_size='14sp', color=(0.3, 0.3, 0.3, 1))

            # Add "Select This Folder" button
            select_btn = Button(text='Diesen Ordner verwenden', size_hint_y=None, height=dp(62), font_size='18sp',
                              background_color=ACTION_BUTTON_COLOR, color=(0.1, 0.1, 0.1, 1))
            select_btn.bind(on_release=lambda x: self._set_output_dir_from_browser(current_dir, rel_path))
            content.add_widget(select_btn)

            # Add "Up" button if not at base directory
            if current_dir != base_dir:
                up_btn = Button(text='.. zurück', size_hint_y=None, height=dp(58), font_size='18sp',
                              background_color=(0.96, 0.96, 0.96, 1), color=(0.1, 0.1, 0.1, 1), halign='left')
                up_btn.bind(texture_size=up_btn.setter('text_size'))
                parent_dir = os.path.dirname(current_dir)
                up_btn.bind(on_release=lambda x: self._navigate_to_directory(parent_dir))
                content.add_widget(up_btn)

            # Add folders
            for folder in folders:
                folder_path = os.path.join(current_dir, folder)
                btn = Button(text=f'[Ordner] {folder}', size_hint_y=None, height=dp(58), font_size='16sp',
                           background_color=(0.96, 0.96, 0.96, 1), color=(0.1, 0.1, 0.1, 1), halign='left')
                btn.bind(texture_size=btn.setter('text_size'))
                btn.bind(on_release=lambda instance, path=folder_path: self._navigate_to_directory(path))
                content.add_widget(btn)

            if not folders:
                content.add_widget(Label(text='(Keine Unterordner)', size_hint_y=None, height=dp(52),
                                       font_size='16sp', color=(0.5, 0.5, 0.5, 1)))

            # Create scrollable view
            scroll_view = ScrollView(size_hint=(1, 1))
            scroll_view.add_widget(content)

            # Create popup layout
            popup_content = BoxLayout(orientation='vertical')
            popup_content.add_widget(path_label)
            popup_content.add_widget(scroll_view)

            # Add bottom button
            close_btn = Button(text='Abbrechen', size_hint_y=None, height=dp(62), font_size='18sp',
                             background_color=(0.96, 0.96, 0.96, 1), color=(0.1, 0.1, 0.1, 1))
            close_btn.bind(on_release=lambda x: self.current_dir_popup.dismiss())
            popup_content.add_widget(close_btn)

            dir_popup = Popup(title='Speicherordner wählen', content=popup_content, size_hint=(0.95, 0.95))
            self.current_dir_popup = dir_popup
            dir_popup.open()

        except Exception as e:
            print(f"DEBUG: Error in directory browser: {e}")
            import traceback
            traceback.print_exc()
            popup = Popup(title='Error', content=Label(text=f'Failed to open browser:\n{e}', font_size='16sp'),
                        size_hint=(0.8, 0.4))
            popup.open()

    def _navigate_to_directory(self, dir_path):
        """Navigate to a new directory in the browser."""
        if hasattr(self, 'current_dir_popup'):
            self.current_dir_popup.dismiss()
        self.show_directory_browser(start_dir=dir_path)

    def _set_output_dir_from_browser(self, full_path, rel_path):
        """Set output directory from browser selection."""
        self.output_dir = full_path
        self.android_output_tree_uri = None
        self.android_output_tree_label = None
        self.status.text = f'Speicherordner: {rel_path}'
        self.output_dir_label.text = f'Ausgabe: {rel_path}'
        if hasattr(self, 'current_dir_popup'):
            self.current_dir_popup.dismiss()

    def _set_output_dir(self, path, popup):
        if path and os.path.isdir(path):
            self.output_dir = path
            self.android_output_tree_uri = None
            self.android_output_tree_label = None
            self.output_dir_label.text = f'Ausgabe: {path}'
            popup.dismiss()
        elif not path:
            self.output_dir = None
            self.android_output_tree_uri = None
            self.android_output_tree_label = None
            self.output_dir_label.text = 'Ausgabe: gleicher Ordner wie die PDF'
            popup.dismiss()
        else:
            error_popup = Popup(title='Error', content=Label(text='Invalid directory path', font_size='16sp'),
                              size_hint=(0.8, 0.3))
            error_popup.open()

    def add_to_merge(self, *args):
        self.merge_mode = True
        if platform == 'android':
            if self._open_android_pdf_picker(multiple=True):
                print("DEBUG: Android SAF PDF picker opened for merge")
                return
            if filechooser:
                try:
                    filechooser.open_file(multiple=True, on_selection=self._file_chosen_multiple)
                    print("DEBUG: Android system file chooser opened for merge")
                    return
                except Exception as e:
                    print(f"DEBUG: Android system file chooser failed for merge: {e}")
            self.show_file_browser()
        else:
            # Fallback to system file chooser on desktop
            if filechooser:
                try:
                    filechooser.open_file(multiple=True, on_selection=self._file_chosen_multiple)
                except Exception as e:
                    print(f"DEBUG: filechooser failed: {e}")
                    # Fallback
                    self.select_file()

    def merge_pdfs(self, *args):
        if not self._can_start_merge():
            self._show_wrapped_popup(
                'Hinweis',
                'Bitte mindestens zwei PDFs hinzufügen oder in "Seiten '
                'bearbeiten" einzelne Seiten löschen bzw. verschieben.',
                size_hint=(0.85, 0.36),
            )
            return

        invalid_files = [path for path in self.merge_list if not is_valid_pdf_path(path)]
        if invalid_files:
            self._show_wrapped_popup(
                'Hinweis',
                'Mindestens eine Merge-Datei ist keine gültige oder lesbare '
                'PDF. Bitte Liste leeren und neu auswählen.',
                size_hint=(0.9, 0.42),
            )
            self.merge_btn.disabled = True
            return

        # Create output path using custom output directory if set
        first_path = self.merge_list[0]
        output_base_dir = self._default_output_base_dir(first_path)
        if not os.path.isdir(output_base_dir):
            popup = Popup(title='Hinweis', content=Label(text='Der Speicherordner ist nicht verfügbar.', font_size='16sp'), size_hint=(0.8, 0.3))
            popup.open()
            return
        out_path = os.path.join(output_base_dir, 'merged.pdf')
        counter = 1
        while os.path.exists(out_path):
            name, ext = os.path.splitext('merged.pdf')
            out_path = os.path.join(output_base_dir, f"{name}_{counter}{ext}")
            counter += 1

        self.status.text = 'Merge läuft...'
        self.merge_btn.disabled = True
        self.merge_progress.value = 0
        merge_paths = list(self.merge_list)
        merge_page_order = list(self.merge_page_order)
        use_page_order = self.merge_page_order_dirty and bool(merge_page_order)
        selected_page_size = self.merge_page_size.text
        page_size = None if selected_page_size == 'Original' else selected_page_size

        def task():
            ordered_pages_dir = None
            try:
                self._set_merge_progress(5, 'Merge wird vorbereitet')
                effective_merge_paths = merge_paths
                if use_page_order:
                    ordered_pages_dir = tempfile.mkdtemp(prefix='pdf-merge-pages-')
                    effective_merge_paths = []
                    total_pages = len(merge_page_order)
                    for index, entry in enumerate(merge_page_order, start=1):
                        page_path = os.path.join(
                            ordered_pages_dir,
                            f'page-{index:05d}.pdf',
                        )
                        if not extract_pdf_page(
                            entry['path'],
                            entry['page'],
                            page_path,
                        ):
                            raise RuntimeError(
                                f'Seite {entry["page"]} aus {entry["name"]} '
                                'konnte nicht übernommen werden.'
                            )
                        effective_merge_paths.append(page_path)
                        progress = 5 + int(index / total_pages * 65)
                        self._set_merge_progress(
                            progress,
                            'Seiten werden vorbereitet',
                        )
                else:
                    self._set_merge_progress(40, 'PDFs werden vorbereitet')

                self._set_merge_progress(75, 'PDFs werden zusammengefügt')
                if not merge_pdfs_with_ghostscript(
                    effective_merge_paths,
                    out_path,
                    page_size=page_size,
                ):
                    raise RuntimeError(
                        'Die PDFs konnten nicht zusammengefügt werden.'
                    )

                in_sizes = sum(os.path.getsize(p) for p in merge_paths)
                out_size = os.path.getsize(out_path)
                saved_location = out_path
                self._set_merge_progress(90, 'Merge wird gespeichert')
                if platform == 'android' and not self.output_dir:
                    if self.android_output_tree_uri:
                        published_location = self._publish_android_output_to_tree(
                            out_path,
                            os.path.basename(out_path),
                        )
                    else:
                        merge_relative_dirs = {
                            self.merge_source_relative_dirs.get(path)
                            for path in merge_paths
                        }
                        merge_relative_dirs.discard(None)
                        relative_dir = (
                            merge_relative_dirs.pop()
                            if len(merge_relative_dirs) == 1
                            else None
                        )
                        published_location = self._publish_android_output(
                            out_path,
                            os.path.basename(out_path),
                            relative_dir=relative_dir,
                        )
                    if not published_location:
                        raise RuntimeError(
                            'Die zusammengefügte PDF konnte nicht gespeichert '
                            'werden. Bitte einen Speicherordner auswählen.'
                        )
                    saved_location = published_location

                def on_done(dt):
                    in_sizes_fmt = format_size(in_sizes)
                    out_size_fmt = format_size(out_size)
                    self.status.text = 'Bereit.'
                    self.merge_progress.value = 100
                    size_note = (
                        'Seitengrössen beibehalten'
                        if page_size is None
                        else f'Alle Seiten auf {page_size} skaliert'
                    )
                    item_count = (
                        len(merge_page_order)
                        if use_page_order
                        else len(merge_paths)
                    )
                    item_label = 'Seiten' if use_page_order else 'PDFs'
                    self.merge_list = []
                    self.merge_display_names = {}
                    self.merge_source_relative_dirs = {}
                    self._cleanup_merge_temp_files()
                    self._reset_merge_page_editor()
                    self._update_merge_status()
                    self.merge_status.text = (
                        f'Merge erfolgreich: {item_count} {item_label} -> '
                        f'{out_size_fmt}\n{size_note}\n'
                        'Merge-Liste leer. Mindestens 2 PDFs hinzufügen.'
                    )

                Clock.schedule_once(on_done, 0)
            except Exception as e:
                error_msg = str(e)
                def on_error(dt):
                    self.status.text = 'Merge fehlgeschlagen'
                    self.merge_progress.value = 0
                    self._show_wrapped_popup(
                        'Fehler',
                        f'Merge fehlgeschlagen:\n\n{error_msg}',
                    )
                    self.merge_btn.disabled = False

                Clock.schedule_once(on_error, 0)
            finally:
                if ordered_pages_dir:
                    shutil.rmtree(ordered_pages_dir, ignore_errors=True)

        threading.Thread(target=task, daemon=True).start()

    def clear_merge_list(self, *args):
        self.merge_list = []
        self.merge_display_names = {}
        self.merge_source_relative_dirs = {}
        self.merge_progress.value = 0
        self._cleanup_merge_temp_files()
        self._reset_merge_page_editor()
        self._update_merge_status()


class PdfCompressorApp(MDApp):
    title = 'Offline PDF'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if KIVYMD_AVAILABLE:
            self.theme_cls.theme_style = "Light"
            self.theme_cls.primary_palette = "BlueGray"
            self.theme_cls.accent_palette = "Green"

    def build(self):
        self.layout = PdfCompressorLayout(size_hint_y=None)
        self.layout.bind(minimum_height=self.layout.setter('height'))
        scroll = ScrollView(size_hint=(1, 1), do_scroll_x=False)
        scroll.add_widget(self.layout)
        return scroll

    def on_pause(self):
        if getattr(self, 'layout', None) and self.layout.compression_running:
            self.layout.status.text = 'Komprimierung läuft im Hintergrund weiter.'
        return True

    def on_stop(self):
        if getattr(self, 'layout', None):
            self.layout._release_compression_wake_lock()
            self.layout._reset_merge_page_editor()


if __name__ == '__main__':
    PdfCompressorApp().run()
