import csv
import json
from pathlib import Path
import pytest
from bioma.peptide import mutation_scan as ms
from bioma.peptide.contracts import sha256


def test_complete_single_mutants_and_fixed_anchor_identity(tmp_path,monkeypatch):
    monkeypatch.setattr(ms,'ROOT',tmp_path)
    args={'sequence':'GMAMSM','fixed_positions':[1,2,4,6]}
    result=ms.scan(**args)
    assert result['summary']['candidate_count']==38
    assert result['summary']['counts_by_chemistry_flag']=={'extra_methionine':2,'introduced_cysteine':2,'retains_anchor_count':34}
    assert ms.validate(result,args)
    file=next(Path(p) for p in result['files'] if p.endswith('.csv'))
    with file.open() as stream:rows=list(csv.DictReader(stream))
    assert len({r['sequence'] for r in rows})==38
    assert all(sum(a!=b for a,b in zip(r['sequence'],args['sequence']))==1 for r in rows)
    assert all(all(r['sequence'][i-1]==args['sequence'][i-1] for i in args['fixed_positions']) for r in rows)
    assert not any('affinity' in k or 'admet' in k.lower() for k in rows[0])
    file.write_text(file.read_text().replace(rows[0]['sequence'],'AAAAAA'))
    result['files'][str(file)]=sha256(file)
    assert not ms.validate(result,args)


def test_rejects_unfixed_methionine_and_unknown_chemistry():
    for sequence,fixed in [('GMAMSM',[1,2]),('GMCXMM',[1,2,5,6]),('GAM',[1,3]),('GMAMSM',[0,2,4,6])]:
        with pytest.raises(ValueError):ms.candidates(sequence,fixed)


def test_summary_and_figure_recomputed(tmp_path,monkeypatch):
    monkeypatch.setattr(ms,'ROOT',tmp_path)
    args={'sequence':'GMAMSM','fixed_positions':[2,4,6]}
    result=ms.scan(**args);summary=next(Path(p) for p in result['files'] if p.endswith('.json'))
    result['summary']['candidate_count']=999;summary.write_text(json.dumps(result['summary']))
    result['files'][str(summary)]=sha256(summary)
    assert not ms.validate(result,args)
    result=ms.scan(**args);chart=next(Path(p) for p in result['files'] if p.endswith('.svg'))
    chart.write_text('<svg/>');result['files'][str(chart)]=sha256(chart)
    assert not ms.validate(result,args)
