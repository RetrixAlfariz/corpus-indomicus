from pathlib import Path
import copy
import io
import json
import zipfile

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw
import pytest

import core
import study


def small_document():
    text = 'BAB II\nPasal 7\n(1) Nilai: 12.000,50; tidak diubah.\n'
    lines=[]; start=0
    for i,t in enumerate(text.splitlines(keepends=True)):
        role,label=core.classify_line(t)
        lines.append(dict(start=start,length=len(t),bbox=[10,10+i*20,250,25+i*20],role=role,label=label,block=i))
        start+=len(t)
    im=Image.new('L',(40,20),255);ImageDraw.Draw(im).line([(1,15),(10,2),(20,18),(39,1)],fill=0,width=2)
    b,spec=core.encode_asset(im);name=spec['encoded_sha256']+'.tif'
    p=dict(number=1,width=300,height=400,text=text,text_sha256=core.sha(text.encode()),lines=lines,vectors=[],
        regions=[dict(bbox=[10,80,50,100],asset=name,type='unclassified_visual',source_pixel_bbox=[0,0,40,20])],
        policy='guarded',source_rotation=0,asset_basis='synthetic',audit={})
    doc=dict(format='indomicus-l1-experiment-v1',converter_version=core.VERSION,source={'sha256':'0'*64,'bytes':100},
             quality={'status':'requires_review','approved_for_source_deletion':False},pages=[p],assets={name:spec})
    return doc,{name:b}


@pytest.mark.parametrize('data',[b'',b'a',b'legal '*1000,bytes(range(256))*2])
def test_zstd_roundtrip(data):
    assert core.zstd(core.zstd(data),True)==data


def test_zstd_bad_frame():
    with pytest.raises(Exception):core.zstd(b'not zstd',True)


@pytest.mark.parametrize('kind',['binary','gray','rgb'])
def test_asset_no_loss(kind):
    rng=np.random.default_rng(71)
    if kind=='binary': a=(rng.integers(0,2,(33,47))*255).astype('uint8');mode='L'
    elif kind=='gray':a=rng.integers(0,256,(33,47),dtype='uint8');mode='L'
    else:a=rng.integers(0,256,(33,47,3),dtype='uint8');mode='RGB'
    im=Image.fromarray(a)
    b,spec=core.encode_asset(im)
    assert Image.open(io.BytesIO(b)).convert(mode).tobytes()==im.tobytes()
    assert spec['bytes']==len(b)


@pytest.mark.parametrize('channelized',[False,True])
def test_roundtrip_and_no_overwrite(tmp_path,channelized):
    doc,assets=small_document();p=tmp_path/'doc.ildr'
    m=core.save_package(p,doc,assets,channelized)
    d,a=core.load_package(p)
    assert d==doc and a==assets
    assert sum(m[k] for k in ['metadata_zstd_bytes','asset_bytes','container_overhead_bytes'])==p.stat().st_size
    with pytest.raises(FileExistsError):core.save_package(p,doc,assets,channelized)


def test_channel_roundtrip():
    d,_=small_document();assert core.uncolumnize(core.columnize(d))==d


def test_empty_channels():
    d,_=small_document();d['pages'][0]['lines']=[]
    assert core.uncolumnize(core.columnize(d))==d


def test_bad_channel_length():
    d,_=small_document();c=core.columnize(d);c['pages'][0]['lines'][0].append(12)
    with pytest.raises(ValueError):core.uncolumnize(c)


@pytest.mark.parametrize('mutation',['text','span','asset','pixel'])
def test_tampering_detected(mutation):
    d,a=small_document();d=copy.deepcopy(d);a=dict(a)
    if mutation=='text':d['pages'][0]['text']+='edited'
    if mutation=='span':d['pages'][0]['lines'][0]['start']=3
    if mutation=='asset':a.clear()
    if mutation=='pixel':next(iter(d['assets'].values()))['pixel_sha256']='0'*64
    with pytest.raises((ValueError,KeyError)):core.validate_document(d,a)


@pytest.mark.parametrize('text,role,label',[
    ('Pasal 7','article','7'),('Pasal 7A','article','7A'),('BAB II','chapter','II'),
    ('Dalam Pasal 7 diatur','text',None),('Pasal I','text',None),
    ('(1) tetap','paragraph','1'),('a. tetap','item','a'),('LAMPIRAN I','appendix',None),
    ('Mengingat:','preamble',None)])
def test_candidate_roles_do_not_correct_ocr(text,role,label):
    assert core.classify_line(text)==(role,label)


def test_crop_union():
    assert core.merge_rects([[0,0,5,5],[4,4,8,8],[20,20,21,21]])==[[0,0,8,8],[20,20,21,21]]


def test_blank_no_text_falls_back():
    boxes,_,info=core.detect_regions(Image.new('L',(300,400),255),(300,400),[])
    assert info['whole_page_fallback'] and boxes==[[0.0,0.0,300.0,400.0]]


def test_manual_protection_is_not_masked():
    im=Image.new('L',(300,400),255);ImageDraw.Draw(im).rectangle([20,30,100,80],fill=0)
    words=[(0,0,300,400,'example',0,0,0)]
    boxes,_,_=core.detect_regions(im,(300,400),words,protected=[[20,30,100,80]])
    assert any(a<=20 and b<=30 and c>=100 and d>=80 for a,b,c,d in boxes)


def test_text_crossing_shape_preserves_entire_component():
    im=Image.new('L',(300,400),255);dr=ImageDraw.Draw(im)
    dr.ellipse([70,120,150,190],outline=0,width=3)
    words=[(0,130,300,170,'text crossing shape',0,0,0)]
    boxes,_,_=core.detect_regions(im,(300,400),words)
    assert any(a<72 and b<123 and c>148 and d>188 for a,b,c,d in boxes)


def test_viewer_escapes_extracted_markup(tmp_path):
    d,a=small_document();p=d['pages'][0];p['text']='<script>alert(1)</script>'
    p['lines']=[{'start':0,'length':len(p['text']),'bbox':[0,0,100,20],'role':'text','label':None,'block':0}]
    core.render_html(d,a,tmp_path/'preview.html')
    s=(tmp_path/'preview.html').read_text()
    assert '<script>alert' not in s and '&lt;script&gt;' in s


def test_archive_bad_path(tmp_path):
    p=tmp_path/'bad.ildr'
    with zipfile.ZipFile(p,'w') as z:z.writestr('../outside','x')
    with pytest.raises(ValueError):core.load_package(p)


def test_parser_and_original_integrity(tmp_path):
    p=tmp_path/'synthetic.pdf';d=fitz.open();page=d.new_page(width=300,height=400)
    page.insert_text((30,40),'Pasal 7',fontsize=14)
    page.insert_text((30,60),'(1) Test 123. No normalization.',fontsize=10)
    page.draw_line((200,300),(260,330),color=(0,0,0),width=2)
    d.save(p);d.close();before=core.sha(p.read_bytes())
    doc,assets,audit=core.encode_pdf(p,tmp_path,'guarded')
    assert core.sha(p.read_bytes())==before
    assert doc['pages'][0]['text']==fitz.open(p)[0].get_text()
    assert doc['quality']['approved_for_source_deletion'] is False
    package=tmp_path/'test.ildr';core.save_package(package,doc,assets)
    restored,_=core.load_package(package)
    assert restored==doc


def test_empty_input_not_zero_saving(tmp_path):
    from types import SimpleNamespace
    root=tmp_path/'input';root.mkdir();out=tmp_path/'output'
    args=SimpleNamespace(input=root,output=out)
    assert study.run(args)==2
    assert json.loads((out/'summary.json').read_text())['saving_percent'] is None


def test_nested_output_rejected(tmp_path):
    from types import SimpleNamespace
    args=SimpleNamespace(input=tmp_path,output=tmp_path/'output')
    with pytest.raises(ValueError):study.run(args)


def test_distribution():
    d=study.distribution([0,10,20]);assert d['median']==10 and d['p10']==2 and d['p90']==18


def test_flow_preserves_every_extracted_character():
    d,a=small_document();d['pages'][0]['audit']={'table_candidates':0,'whole_page_fallback':False}
    flow=core.flow_document(d)
    assert all(l['bbox'] is None for l in flow['pages'][0]['lines'])
    assert flow['pages'][0]['text']==d['pages'][0]['text']
    core.validate_document(flow,a)


def test_table_flow_keeps_layout():
    d,_=small_document();d['pages'][0]['audit']={'table_candidates':1}
    flow=core.flow_document(d)
    assert flow['pages'][0]['layout_mode']=='fixed_table_or_fallback'
    assert flow['pages'][0]['lines']==d['pages'][0]['lines']


@pytest.mark.parametrize('field,value',[('width',float('inf')),('height',-1),('number','<script>')])
def test_invalid_page_geometry(field,value):
    d,a=small_document();d['pages'][0][field]=value
    with pytest.raises(ValueError):core.validate_document(d,a)


def test_vector_policy_preserves_crossing_nonrule_shape():
    im=Image.new('L',(300,400),255);dr=ImageDraw.Draw(im)
    dr.ellipse([70,120,150,190],outline=0,width=3)
    words=[(0,130,300,170,'text crossing shape',0,0,0)]
    boxes,_,_=core.detect_regions(im,(300,400),words,policy='vector_candidate')
    assert any(a<72 and b<123 and c>148 and d>188 for a,b,c,d in boxes)


def test_reflow_viewer_escapes_text(tmp_path):
    d,a=small_document();d['pages'][0]['text']='<script>'
    d['pages'][0]['lines']=[{'start':0,'length':8,'role':'text'}]
    core.render_flow_html(d,a,tmp_path/'v.html')
    assert '<script>' not in (tmp_path/'v.html').read_text()
