"""从 Dify DSL 中提取 LLM 提示词与参数提取器定义，生成 backend/pipeline/prompts.py"""
import yaml

data = yaml.safe_load(open('绿碳能源投运专家.yml', encoding='utf-8'))
nodes = {n['id']: n['data'] for n in data['workflow']['graph']['nodes']}

# id -> python 变量名
NAME_MAP = {
    'llm': 'PROJECT_SUMMARY',           # 项目信息处理
    '1779960894550': 'ANALYSIS_ENGINEER',  # 综合分析工程师
    '1780037885729': 'LOW_CARBON_ENGINEER',  # 绿色低碳工程师
    '1780300554890': 'REPORT_GENERATOR',  # 技术方案生成
    '1779949359051': 'SATELLITE_VISION',  # 卫星图结构化识别
    '1779958615816': 'CAD_VISION',        # CAD图纸视觉识别
    '1780133837019': 'BILL_ANALYSIS',     # 电价
}

out = ['"""全部 LLM 提示词：由 绿碳能源投运专家.yml 原样提取生成，请勿随意改动数值规则。\n\n'
       '占位符仍保留 Dify 语法 {{#node_id.var#}}，由 pipeline.engine 在运行时替换。\n"""\n']

for node_id, var in NAME_MAP.items():
    d = nodes[node_id]
    title = d.get('title', '')
    for tpl in d.get('prompt_template', []):
        role = tpl['role'].upper()
        text = tpl['text']
        out.append(f'# ---- {title} ({node_id}) [{role}] ----')
        out.append(f'{var}_{role} = {text!r}\n')

# 参数提取器
pe = nodes['1779949923361']
out.append('# ---- 投运测算参数提取器 (1779949923361) ----')
out.append(f'PARAM_EXTRACTOR_INSTRUCTION = {pe["instruction"]!r}\n')
params = [
    {'name': p['name'], 'description': p['description'],
     'required': p.get('required', False), 'type': p.get('type', 'number')}
    for p in pe['parameters']
]
out.append(f'PARAM_EXTRACTOR_PARAMETERS = {params!r}\n')

# 开场白与建议问题
feats = data['workflow']['features']
out.append(f'OPENING_STATEMENT = {feats["opening_statement"]!r}\n')
out.append(f'SUGGESTED_QUESTIONS = {feats["suggested_questions"]!r}\n')

path = 'green-carbon-agent/backend/pipeline/prompts.py'
with open(path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
compile(open(path, encoding='utf-8').read(), path, 'exec')
print('OK', path)
