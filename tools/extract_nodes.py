import yaml, os, json

data = yaml.safe_load(open('绿碳能源投运专家.yml', encoding='utf-8'))
nodes = data['workflow']['graph']['nodes']
os.makedirs('_extracted', exist_ok=True)
for n in nodes:
    d = n.get('data', {})
    if d.get('type') == 'code':
        title = d.get('title', n['id'])
        fn = os.path.join('_extracted', f"{n['id']}_{title}.py")
        with open(fn, 'w', encoding='utf-8') as f:
            f.write(d['code'])
        compile(d['code'], fn, 'exec')
        print('OK', fn, len(d['code']))
