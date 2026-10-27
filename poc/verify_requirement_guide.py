"""Real browser/API/SQLite guide acceptance with controlled jobs, no Docker or model calls.

Run against a new dedicated --root. The fixture claims only jobs in that new
SQLite database; it never runs a production Runner or touches other workspaces.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import httpx
from playwright.sync_api import expect, sync_playwright

from agent_workbench.execution import Executor
from agent_workbench.settings import Settings
from agent_workbench.store import Store, jobs
from agent_workbench.workspace import git, snapshot, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists():
        raise ValueError('Use a new dedicated verification directory')
    root.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[1]
    report = {'status': 'RUNNING', 'model_inference': False, 'real_docker': False,
              'controlled_jobs': True, 'real_api_and_sqlite': True, 'checks': []}
    source = root / 'source'
    source.mkdir()
    (source / 'main.txt').write_text('original\n', encoding='utf-8')
    for argv in [('init', '-b', 'main'), ('config', 'user.name', 'Guide Fixture'),
                 ('config', 'user.email', 'fixture@example.invalid'), ('add', '--all'),
                 ('-c', 'commit.gpgsign=false', 'commit', '-m', 'guide fixture')]:
        git(source, *argv)
    original = snapshot(source)
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    config = root / 'workbench.toml'
    config.write_text('[workbench]\nuse_sqlite=true\ndata_dir="data"\n'
                      f'port={port}\nmodel="opencode/glm-5.3-flash-free"\n'
                      'web_dir=' + json.dumps(str(repo / 'web/dist')) + '\n', encoding='utf-8')
    settings = Settings.load(config_file=config)
    base = f'http://127.0.0.1:{port}'
    env = {name: value for name, value in os.environ.items() if not name.startswith('AWB_')}
    env.update(PYTHONPATH=str(repo / 'src'), PYTHONUTF8='1')
    logfile = (root / 'api.log').open('w', encoding='utf-8')
    server = subprocess.Popen([sys.executable, '-m', 'agent_workbench.server', '--config', str(config)],
                              cwd=repo, env=env, stdout=logfile, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url=base + '/api/v1', trust_env=False, timeout=15,
                         headers={'Authorization': 'Bearer ' + settings.access_token})
    store = None

    def check(name):
        report['checks'].append(name)
        print(name, flush=True)

    def get(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()

    def post(path, data):
        response = client.post(path, json=data)
        response.raise_for_status()
        return response.json()

    try:
        for _ in range(100):
            try:
                if client.get('/session').status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        else:
            raise AssertionError('API did not start')
        store = Store(settings.database_url)
        executor = Executor(settings)
        post('/projects', {'name': '向导验收项目', 'source_path': str(source)})

        def finish(action=None, step=None, fail=False):
            deadline = time.monotonic() + 5
            job = None
            while job is None and time.monotonic() < deadline:
                job = store.claim('guide_fixture')
                if job is None:
                    time.sleep(.05)
            assert job and (not action or job['action'] == action), job
            assert not step or job['input']['step_key'] == step
            workspace = Path(job['workspace']['path'])
            result = {}
            if job['action'] == 'prepare':
                result = executor.execute(job, lambda *_: None, threading.Event())
            elif job['action'] in ('start', 'stop', 'reconcile'):
                result = executor.observe(workspace, 'RUNNING' if job['action'] == 'start' else 'STOPPED')
            else:
                result = executor.observe(workspace)
                if job['action'] == 'workflow':
                    key = job['input']['step_key']
                    output = ({'summary': '受控分析：检查 main.txt；未调用模型', 'impacted_paths': ['main.txt'],
                               'plan': '限定在样例文件内修改', 'risks': []} if key == 'analysis' else
                              {'summary': '受控开发完成；未调用模型', 'verification_notes': '受控验证记录'} if key == 'development' else
                              {'command': ['node', '--test'], 'exit_code': 0} if key == 'verification' else
                              {'summary': '受控独立评审；未调用模型', 'findings': []} if key.startswith('model_review_') else
                              job['input']['request'])
                    result['workflow_output'] = output
                elif job['action'] == 'materials':
                    key = job['input']['step_key']
                    output = ({'title': '受控概要设计', 'markdown': '# 样例\n仅验证界面，不代表真实模型文档。'} if key == 'summary_design' else
                              {'title': '受控测试单', 'environment': '受控测试环境', 'cases': [
                                  {'id': 'F1', 'title': '检查样例', 'preconditions': '已准备', 'steps': ['受控检查'],
                                   'expected': '界面更新', 'required': True}]} if key == 'test_plan' else job['input']['data'])
                    result['material_output'] = output
            store.finish(job['id'], 'guide_fixture', job['epoch'], 'FAILED' if fail else 'SUCCEEDED', result,
                         '受控失败：请修复原因后重新提交' if fail else None)
            observed = store.get(jobs, job['id'])
            assert observed['status'] == ('FAILED' if fail else 'SUCCEEDED'), observed
            return job

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1100})
            page.set_default_timeout(15000)
            errors = []
            page.on('pageerror', lambda failure: errors.append(str(failure)))
            page.goto(base)
            page.get_by_label('本机访问令牌', exact=True).fill(settings.access_token)
            page.get_by_role('button', name='进入工作台', exact=True).click()
            page.get_by_role('button', name='＋ 新建需求', exact=True).click()
            dialog = page.get_by_role('dialog')
            dialog.get_by_label('需求名称', exact=True).fill('把需求一步一步推进')
            dialog.get_by_label('需求背景', exact=True).fill('受控浏览器验收，不调用模型或 Docker。')
            dialog.get_by_label('验收标准', exact=True).fill('当前步骤清晰，不能跳过前置确认。')
            dialog.get_by_role('button', name='创建需求', exact=True).click()
            expect(dialog).not_to_be_visible()
            identity = get('/requirements')[0]['id']
            endpoint = '/requirements/' + identity
            guide = page.get_by_role('region', name='需求推进步骤')
            steps = guide.locator('ol > li > button')

            def at(index):
                expect(steps.nth(index)).to_have_attribute('aria-current', 'step')
                for later in range(index + 1, 6):
                    expect(steps.nth(later)).to_be_disabled()

            at(0)
            for title in ('环境与手动工具', '代码差异与自审记录'):
                expect(page.locator('details').filter(has=page.get_by_text(title, exact=True))).not_to_have_attribute('open', '')
            expect(page.get_by_role('button', name='核对验收清单', exact=True)).not_to_be_visible()
            page.screenshot(path=str(root / '01-new-requirement-desktop.png'), full_page=True)
            check('new_requirement_has_one_preparation_action_and_locks_future_stages')
            page.get_by_role('button', name='创建工作区，开始准备', exact=True).click()
            finish('prepare')
            expect(page.get_by_role('button', name='启动环境，进入分析', exact=True)).to_be_enabled()
            page.get_by_role('button', name='启动环境，进入分析', exact=True).click()
            finish('start')
            at(1)
            panel = page.locator('.workflow-panel')
            panel.get_by_role('button', name='开启默认流程', exact=True).click()
            panel.get_by_label('验证命令', exact=True).fill('node --test')
            panel.get_by_role('button', name='保存并开启本轮', exact=True).click()
            expect(panel.get_by_role('heading', name='下一步：只读全景分析', exact=True)).to_be_visible()
            panel.get_by_role('button', name='运行：只读全景分析', exact=True).click()
            finish('workflow', 'analysis', fail=True)
            expect(page.get_by_role('heading', name='只读分析未完成', exact=True)).to_be_visible()
            at(1)
            expect(panel.get_by_role('button', name='运行：只读全景分析', exact=True)).to_be_enabled()
            panel.get_by_role('button', name='运行：只读全景分析', exact=True).click()
            finish('workflow', 'analysis')
            expect(panel.get_by_role('heading', name='下一步：范围与方案确认', exact=True)).to_be_visible()
            expect(panel.get_by_text('限定在样例文件内修改', exact=True)).to_be_visible()
            expect(panel.get_by_role('button', name='确认本步通过', exact=True)).to_be_disabled()
            check('failed_step_stays_current_can_retry_and_scope_requires_human_reason')
            page.screenshot(path=str(root / '02-analysis-and-scope-desktop.png'), full_page=True)
            for key in ('scope', 'development', 'verification', 'model_review_0', 'model_review_1', 'resolution', 'self_review'):
                flow = get(endpoint + '/workflow')['current']
                assert flow['next_step'] == key
                node = next(item for item in flow['nodes'] if item['key'] == key)
                expect(panel.get_by_role('heading', name='下一步：' + node['label'], exact=True)).to_be_visible()
                if key in ('scope', 'resolution', 'self_review'):
                    panel.get_by_label('确认依据', exact=True).fill('已检查受控样例结果，仅用于验收')
                    button = panel.get_by_role('button', name='确认本步通过', exact=True)
                else:
                    button = panel.get_by_role('button', name='运行：' + node['label'], exact=True)
                if key == 'development':
                    at(2)
                    steps.nth(1).click()
                    expect(page.get_by_role('button', name='返回当前步骤', exact=True)).to_be_visible()
                    expect(panel.get_by_role('button', name='运行：' + node['label'], exact=True)).not_to_be_visible()
                    page.get_by_role('button', name='返回当前步骤', exact=True).click()
                if key == 'model_review_0':
                    at(3)
                    page.reload()
                    at(3)
                    expect(page.get_by_label('代码差异', exact=True)).to_be_visible()
                button.click()
                expect(button).to_be_disabled()
                finish('workflow', key)
            at(4)
            expect(panel).not_to_be_visible()
            check('ordered_flow_advances_automatically_history_is_readable_and_reload_resumes_review')
            material = page.locator('.material-panel')
            material.get_by_role('button', name='开始材料阶段', exact=True).click()
            expect(material.get_by_role('button', name='生成概要设计', exact=True)).to_be_enabled()
            material.get_by_role('button', name='生成概要设计', exact=True).click()
            finish('materials', 'summary_design')
            check('self_review_unlocks_design_without_marking_tests_or_delivery_complete')

            def record(label, key, extra=None):
                material.get_by_role('button', name=label, exact=True).click()
                form = page.get_by_role('dialog')
                form.get_by_label('评审人' if key == 'supervisor_review' else '登记人员', exact=True).fill('受控登记人')
                form.get_by_label('意见与说明', exact=True).fill('受控记录，不代表真实外部审批或实际业务测试')
                form.get_by_label('证据引用', exact=True).fill('fixture://guide-acceptance')
                if extra:
                    extra(form)
                form.get_by_role('button', name='保存记录', exact=True).click()
                expect(form).not_to_be_visible()
                finish('materials', key)

            record('登记主管评审', 'supervisor_review')
            record('登记性能测试判断', 'performance_decision',
                   lambda form: form.get_by_label('是否需要性能测试', exact=True).select_option('NOT_REQUIRED'))
            material.get_by_role('button', name='生成功能测试单', exact=True).click()
            finish('materials', 'test_plan')

            def case_result(form):
                form.get_by_label('实际测试环境', exact=True).fill('受控样例')
                form.get_by_role('checkbox', name='登记 F1', exact=True).check()
                form.get_by_label('结果 · F1', exact=True).select_option('PASS')
                form.get_by_label('实际结果 · F1', exact=True).fill('受控测试记录，仅验收界面')
                form.get_by_label('用例证据 · F1', exact=True).fill('fixture://case-F1')

            record('登记测试执行', 'test_execution', case_result)
            at(4)
            record('确认材料阶段验收', 'acceptance')
            at(5)
            page.get_by_role('button', name='核对验收清单', exact=True).click()
            expect(page.get_by_text('仍有待补齐的执行证据', exact=True)).to_be_visible()
            check('only_registered_material_acceptance_unlocks_delivery_audit_keeps_fixture_evidence_incomplete')
            page.get_by_role('button', name='查看材料与测试记录', exact=True).click()
            expect(material.get_by_text('材料阶段已验收', exact=True).first).to_be_visible()
            page.get_by_role('button', name='返回当前步骤', exact=True).click()
            page.screenshot(path=str(root / '03-delivery-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(root / '04-guide-mobile.png'), full_page=True)
            check('desktop_and_mobile_have_no_horizontal_overflow_or_page_errors')
            steps.nth(1).click()
            panel.get_by_role('button', name='修订需求', exact=True).click()
            dialog = page.get_by_role('dialog')
            dialog.get_by_label('验收标准', exact=True).fill('需求变更后重新分析')
            dialog.get_by_label('变更原因', exact=True).fill('受控修订，验证旧结论失效')
            dialog.get_by_role('button', name='保存新版本', exact=True).click()
            expect(dialog).not_to_be_visible()
            at(1)
            expect(panel.get_by_role('button', name='新建流程轮次', exact=True)).to_be_enabled()
            panel.get_by_role('button', name='新建流程轮次', exact=True).click()
            panel.get_by_role('button', name='保存并开启本轮', exact=True).click()
            expect(panel.get_by_role('heading', name='下一步：只读全景分析', exact=True)).to_be_visible()
            check('requirement_revision_returns_to_analysis_and_preserves_previous_round')
            # Selecting another requirement must not reuse the old guide state.
            other = post('/requirements', {'project_id': get('/projects')[0]['id'], 'title': '另一个新需求'})
            page.get_by_role('button', name='另一个新需求', exact=False).click()
            at(0)
            assert get('/requirements/' + other['id'])['workspace']['environment'] == 'NOT_CREATED'
            assert snapshot(source) == original
            assert not errors, errors
            check('switching_requirements_resets_view_and_original_source_is_unchanged')
            report.update(status='PASSED', completed_checks=len(report['checks']), page_errors=errors)
            browser.close()
    except BaseException as failure:
        report.update(status='FAILED', completed_checks=len(report['checks']), error_type=type(failure).__name__)
        raise
    finally:
        client.close()
        if store:
            store.engine.dispose()
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        logfile.close()
        write_json(root / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
