"""Read-only inventory diagnostics for the local PDF study."""
from collections import Counter
import inspect
from pathlib import Path
import pymupdf

files = list(Path('data').rglob('*'))
print('Data suffix inventory:', Counter(p.suffix.lower() for p in files if p.is_file()))
pdfs = [p for p in files if p.is_file() and p.suffix.lower() == '.pdf']
print('PDF count/bytes:', len(pdfs), sum(p.stat().st_size for p in pdfs))
print('PyMuPDF version:', pymupdf.__version__)
print('rewrite_images available:', hasattr(pymupdf.Document, 'rewrite_images'))
if hasattr(pymupdf.Document, 'rewrite_images'):
    print(inspect.signature(pymupdf.Document.rewrite_images))
    print(pymupdf.Document.rewrite_images.__doc__)

import pyarrow.parquet as pq
profile = Path('reports/local-study-20260916')
if (profile / 'object_profile.parquet').exists():
    objects = pq.read_table(profile / 'object_profile.parquet').to_pylist()
    print('Object errors:', [o for o in objects if o.get('error')][:5])
    print('Font types:', Counter(o.get('font_type') for o in objects if o.get('font_type')))
    print('Font examples:', [o for o in objects if o.get('font_type')][:2])
    pages = pq.read_table(profile / 'page_profile.parquet').to_pylist()
    print('Page flags:', {k: sum(bool(p[k]) for p in pages) for k in
          ('full_page_raster', 'vector_heavy', 'large_map_or_table_candidate', 'invisible_text_chars')})
    images = pq.read_table(profile / 'image_profile.parquet').to_pylist()
    print('Image colorspace/BPC:', Counter((i['colorspace'], i['bits_per_component']) for i in images))
    print('DPI min/max:', min(i['dpi_x'] for i in images), max(i['dpi_x'] for i in images))
    with pymupdf.open(pdfs[0]) as doc:
        print('First page fonts:', doc[0].get_fonts(full=True))
        for font in doc[0].get_fonts(full=True)[:2]:
            print(doc.xref_object(font[0]))
