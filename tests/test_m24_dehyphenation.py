"""M24-only deterministic joining; shared extraction/localization stay unchanged."""
import difflib
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from pdf_annotator.domain.structured_text import normalize_page_text
from .test_structured_text import native, legacy_flattened


@pytest.mark.parametrize("original,expected", [
    ("ana-\nlizzare", "analizzare"), ("riguar-\ndano", "riguardano"),
    ("chia- \t\r\n\tmante", "chiamante"), ("città-\nalta", "cittàalta"),
    ("a-\nb-\nc", "abc"), ("top-down  bottom-up", "top-down  bottom-up"),
    # Genuine compounds at a line end are deliberately subject to the same rule.
    ("soffitto-\npavimento", "soffittopavimento"),
    ("12-\n34", "12- 34"), ("x-\n2", "x- 2"), ("2-\nx", "2- x"),
    ("+-\nx", "+- x"), ("x-\n+", "x- +"), ("_-\nx", "_- x"),
    ("²-\nx", "²- x"), ("x-\n²", "x- ²"), ("a-\n\nb", "a-  b"),
    ("a -\nb", "a - b"), ("x = -\nvalue;", "x = - value;"),
    ("x -= 1;\n--x;", "x -= 1; --x;"),
    ("  non-locali    a\nb  ", "  non-locali    a b  "),
])
def test_minimal_rule(original, expected):
    assert normalize_page_text(original) == expected


def test_school_all_pages_code_and_localization():
    path = Path(r"C:\Users\matte\Desktop\0000.000_000_SezA_UDA1.pdf")
    if not path.exists():
        pytest.skip("Owner's school proof unavailable")
    originals = native(path)
    assert len(originals) == 19
    deletions = 0
    for text in originals:
        before = ''.join(legacy_flattened(text).split())
        after = ''.join(normalize_page_text(text).split())
        changes = [op for op in difflib.SequenceMatcher(None,before,after,autojunk=False).get_opcodes() if op[0]!='equal']
        for tag, start, end, target_start, target_end in changes:
            assert tag == 'delete' and before[start:end] == '-'
            deletions += 1
        for word in re.findall(r"\b\w+-\w+\b",text):
            assert word in normalize_page_text(text)
    assert deletions == 55
    assert 'analizzare' in normalize_page_text(originals[3])
    assert 'riguardano' in normalize_page_text(originals[4])
    assert 'chiamante' in normalize_page_text(originals[4])
    code = r"""
import sys,re,pymupdf
from pdf_annotator.worker.review_localization_text import base_textpage,EXTRACTION_FLAGS
from pdf_annotator.worker.review_localization import localize_document
from pdf_annotator.domain.structured_text import normalize_page_text
from pdf_annotator.domain.structured_text import _LINE_HYPHEN
count=0
joins=0
code_font_joins=[]
with pymupdf.open(sys.argv[1]) as doc:
    for page in doc:
        raw=base_textpage(page,EXTRACTION_FLAGS).extractDICT()
        for block in raw['blocks']:
            if block['type'] != 0: continue
            parts=[]
            fonts=[]
            for line in block['lines']:
                for span in line['spans']:
                    parts.append(span['text'])
                    fonts.extend([span['font']] * len(span['text']))
                    count += 'LucidaSansTypewriter' in span['font']
                parts.append('\n')
                fonts.append('')
            text=''.join(parts)
            for match in _LINE_HYPHEN.finditer(text):
                if not (match[1].isalpha() and match[2].isalpha()): continue
                joins += 1
                if any('LucidaSansTypewriter' in fonts[pos] for pos in (match.start(),match.end())):
                    code_font_joins.append((page.number+1,text[match.start()-2:match.end()+5]))
    for index,word,original in [(3,'analizzare','ana-\nlizzare'),(4,'riguardano','riguar-\ndano'),(4,'chiamante','chia-\nmante')]:
        result,=localize_document(doc,[dict(operation='delete',page_index=index,original_text=word,context_before=None,context_after=None,insert_anchor=None)],base_only=True)
        assert result.applicable and result.candidate_count == 1 and len(result.quads) == 2
        assert result.matched_text == original
assert count == 900 and joins == 55
# This typewriter-font identifier is in prose, not a C++ listing.
assert code_font_joins == [(13,'qua-\ndrato')]
print('CODE_SPANS_CHECKED',count,'PROSE_IDENTIFIER_JOIN',code_font_joins,'LOCALIZATION_CASES',3)
"""
    result=subprocess.run([sys.executable,'-c',code,str(path)],check=False,capture_output=True,encoding='utf-8',timeout=30)
    assert result.returncode == 0, result.stderr
    print(result.stdout.strip())


def test_novel_json_before_after_identical():
    path=Path(r"C:\Users\matte\Desktop\MATTEO\Documenti definitivi\Due Ragni in un Boccale.pdf")
    if not path.exists():
        pytest.skip("Owner's novel unavailable")
    originals=native(path)
    assert len(originals)==44
    before={'pages':[{'page':i+1,'text':legacy_flattened(t)} for i,t in enumerate(originals)]}
    after={'pages':[{'page':i+1,'text':normalize_page_text(t)} for i,t in enumerate(originals)]}
    assert json.dumps(before,ensure_ascii=False) == json.dumps(after,ensure_ascii=False)
    assert all(before['pages'][i] == after['pages'][i] for i in range(20,24))
    assert 'queste astrazioni e di come in loro abbiamo riposto speranze deformi di onnicomprensività' in ' '.join(after['pages'][24]['text'].split())
