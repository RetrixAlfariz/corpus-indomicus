"""Verify an L1 package and export a standalone reading view or raw text."""
import argparse
from pathlib import Path
import core


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('package',type=Path)
    p.add_argument('--html',type=Path)
    p.add_argument('--text',type=Path)
    p.add_argument('--fixed',action='store_true',help='Positional review instead of readable reflow')
    a=p.parse_args()
    for target in [a.html,a.text]:
        if target and target.exists():p.error(f'refusing to overwrite {target}')
    doc,assets=core.load_package(a.package)
    if a.html:
        (core.render_html if a.fixed else core.render_flow_html)(doc,assets,a.html)
    if a.text:
        a.text.write_text(''.join(p['text'] for p in doc['pages']),encoding='utf-8',newline='')
    print(f"Verified: {len(doc['pages'])} pages, {len(assets)} assets; quality remains {doc['quality']['status']}.")

if __name__=='__main__':main()
