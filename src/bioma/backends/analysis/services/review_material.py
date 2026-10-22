"""Explicit, bounded review projections; never imply full semantic inspection."""
import csv
import gzip
import math
import tarfile
from pathlib import Path


def project_material(path: Path, digest: str):
    name = path.name.lower()
    if path.suffix.lower() in {'.png','.jpg','.jpeg'}:
        from PIL import Image
        with Image.open(path) as image:
            dimensions=list(image.size);mode=image.mode;image_format=image.format
            image.verify()
        return {'sha256':digest,'bytes':path.stat().st_size,'pixel_dimensions':dimensions,
                'mode':mode,'format':image_format,
                'scope':'Image format integrity and dimensions only; pixels, labels and scientific meaning are not visually reviewed'}
    if name.endswith('.pdf'):

        from pypdf import PdfReader
        reader = PdfReader(path)
        if not reader.pages or len(reader.pages) > 80:
            raise ValueError('PDF page count exceeds bounded review capacity or is empty')
        pages = []
        total = 0
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            total += len(text.encode('utf-8'))
            if total > 60_000:
                raise ValueError('PDF extracted text exceeds bounded review capacity')
            pages.append({'page': index, 'text': text})
        return {'sha256': digest, 'page_count': len(pages), 'pages': pages,
                'scope': 'text extracted from every PDF page; figures, raster text, glyph appearance and layout are not visually verified'}
    table = name.endswith(('.csv', '.tsv', '.csv.gz', '.tsv.gz'))
    if table:
        opener = gzip.open if name.endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8-sig', newline='') as stream:
            rows = csv.reader(stream, delimiter='\t' if '.tsv' in name else ',')
            header = next(rows, [])
            if len(header) > 1000 or sum(map(len, header)) > 20000:
                raise ValueError('Table header exceeds bounded review capacity')
            stats = [dict(numeric=0, missing=0, other=0, minimum=None, maximum=None) for _ in header]
            examples, count = [], 0
            for row in rows:
                count += 1
                if len(row) != len(header):
                    raise ValueError(f'Inconsistent table width in {path.name}, row {count}')
                if len(examples) < 3:
                    examples.append([value[:160] for value in row])
                for value, stat in zip(row, stats):
                    if value in ('', 'NA', 'NaN', 'nan'):
                        stat['missing'] += 1
                        continue
                    try:
                        number = float(value)
                    except ValueError:
                        stat['other'] += 1
                        continue
                    if not math.isfinite(number):
                        stat['other'] += 1
                        continue
                    stat['numeric'] += 1
                    stat['minimum'] = number if stat['minimum'] is None else min(number, stat['minimum'])
                    stat['maximum'] = number if stat['maximum'] is None else max(number, stat['maximum'])
        return {'sha256': digest, 'scope': 'all rows parsed for dimensions and column statistics; only first 3 rows shown, cells capped at 160 characters; no row-level semantic validation',
                'rows': count, 'columns': len(header), 'header': header,
                'column_statistics': stats, 'first_rows': examples}
    if name.endswith(('.txt.gz', '.soft.gz', '.fasta.gz', '.fa.gz')):
        preview = []
        lines = 0
        characters = 0
        with gzip.open(path, 'rt', encoding='utf-8-sig') as stream:
            for line in stream:
                lines += 1
                characters += len(line)
                if len(preview) < 12:
                    preview.append(line.rstrip()[:1000])
        return {'sha256': digest, 'compressed_bytes': path.stat().st_size,
                'lines': lines, 'decoded_characters': characters, 'first_lines': preview,
                'scope': 'all compressed text decoded and counted; only first 12 lines shown, capped at 1000 characters each; no full semantic validation'}
    if path.suffix.lower() in {'.rda', '.rdata', '.rds', '.h5', '.h5ad', '.loom'}:
        return {'sha256': digest, 'bytes': path.stat().st_size,
                'scope': 'file identity and size only; binary/structured contents not independently semantically reviewed; assess executed reader and reported checks separately'}
    if name.endswith(('.tar', '.tar.gz', '.tgz')):
        with tarfile.open(path, mode='r:*') as archive:
            members = archive.getmembers()
        if len(members) > 10_000:
            raise ValueError('Archive member count exceeds bounded review capacity')
        regular = [member for member in members if member.isfile()]
        return {
            'sha256': digest,
            'bytes': path.stat().st_size,
            'member_count': len(members),
            'regular_file_count': len(regular),
            'total_regular_file_bytes': sum(member.size for member in regular),
            'members': [
                {'name': member.name[:1000], 'bytes': member.size,
                 'kind': 'file' if member.isfile() else 'directory' if member.isdir() else 'other'}
                for member in members
            ],
            'scope': (
                'complete archive header and member inventory with whole-file SHA-256; '
                'member contents are not independently semantically reviewed; assess the '
                'executed reader, input checks and reported results separately'
            ),
        }
    return None
