"""Exercise global settings and workspace assignments against an isolated real API.

/tmp/sapper-ui-venv/bin/python tests/check_settings_browser.py --browser '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'lib'))
from sapper_web import Console, WebServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser')
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    checks = []

    def check(name, value):
        if not value:
            raise AssertionError(name)
        checks.append(name)

    with tempfile.TemporaryDirectory(prefix='sapper-settings-browser-') as directory:
        base = Path(directory)
        app = Console(base / 'tasks', base / 'state')
        app.panes = lambda: []
        app.workspaces._capabilities = {'available': False, 'skill_isolation': False}
        first = app.create_project({'name': 'crypto-lab', 'goal': '密码分析工作区'})
        second = app.create_project({'name': 'web-lab', 'goal': 'Web 审计工作区'})
        model = app.save_profile({'name': 'Analysis model', 'model': 'fixture-model'})
        skill = base / 'review'
        skill.mkdir()
        (skill / 'SKILL.md').write_text('---\nname: review\ndescription: fixture\n---\nReview fixture.\n')
        ext = app.workspaces.extensions
        ext.update(None, {'action': 'save', 'kind': 'skills', 'name': 'review', 'path': str(skill), 'expected_revision': 0})
        server = WebServer(('127.0.0.1', 0), app, prefix='/sapper')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=args.browser)
                context = browser.new_context(viewport={'width': 1536, 'height': 1050}, color_scheme='light')
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/sapper/')
                page.locator('#access-token').fill(app.token_path.read_text().strip())
                page.locator('#login-form button[type=submit]').click()
                expect(page.locator('#workspace')).to_be_visible()
                page.locator('#profiles-button').click()
                expect(page.locator('#models-title')).to_have_text('全局设置')
                check('settings stylesheet loads behind URL prefix', page.locator('.settings-layout').evaluate('e => getComputedStyle(e).display') == 'grid')
                expect(page.locator('#settings-skill-count')).to_have_text('1')
                check('opens globally from overview without selecting a workspace', page.evaluate('state.project === null'))
                page.locator('[data-settings-tab=mcp]').click()
                expect(page.locator('#settings-mcp-panel')).to_be_visible()
                page.locator('#extensions-mcp-add').click()
                config_input = page.locator('#extensions-mcp-form textarea')
                import_button = page.locator('#extensions-mcp-import')
                revision = ext.view()['revision']
                for source, message in [
                    ('', '请先在 Server JSON 中粘贴完整'),
                    ('{"mcpServers":', 'JSON 格式不正确或内容不完整'),
                    ('[]', 'MCP 配置必须是 JSON 对象'),
                    ('{"command":"fixture"}', '批量导入需要包含'),
                    ('{"mcpServers":{}}', '批量导入需要包含'),
                    ('{"mcpServers":{"invalid":{}}}', 'command 必须是非空文本')]:
                    config_input.fill(source)
                    import_button.click()
                    expect(page.locator('#extensions-mcp-error')).to_contain_text(message)
                    check('invalid import does not write resource library: ' + (source or 'empty'), ext.view()['revision'] == revision)
                    check('invalid import retains input: ' + (source or 'empty'), config_input.input_value() == source)
                batch = {'mcpServers': {'import-one': {'command': 'fixture-one'}, 'import-two': {'type': 'http', 'url': 'https://example.test/mcp'}}}
                config_input.fill(json.dumps(batch))
                import_button.click()
                expect(page.locator('#extensions-mcp-list .resource-row')).to_have_count(2)
                check('batch import succeeds without a separate name', len(ext.view()['mcp']) == 2)
                original_id = next(r['id'] for r in ext.view()['mcp'] if r['name'] == 'import-one')
                page.locator('#extensions-mcp-add').click()
                config_input.fill('{"mcpServers":{"import-one":{"command":"updated-fixture"}}}')
                import_button.click()
                expect(page.locator('#extensions-mcp-list')).to_contain_text('updated-fixture')
                check('reimport updates matching server without duplication', len(ext.view()['mcp']) == 2 and next(r['id'] for r in ext.view()['mcp'] if r['name'] == 'import-one') == original_id)
                for name in ['import-one', 'import-two']:
                    row = page.locator('#extensions-mcp-list .resource-row').filter(has=page.locator('strong', has_text=name))
                    row.get_by_role('button', name='移除', exact=True).click()
                    page.locator('#confirm-accept').click()
                    expect(row).to_have_count(0)
                page.locator('#extensions-mcp-add').click()
                page.locator('#extensions-mcp-form input[name=name]').fill('exa')
                page.locator('#extensions-mcp-form textarea').fill('{"type":"http","url":"https://example.test/mcp"}')
                page.locator('#extensions-mcp-form button[type=submit]').click()
                expect(page.locator('#extensions-mcp-list .resource-info strong')).to_have_text('exa')
                check('global resource saves through prefixed real API', ext.view()['mcp'][0]['name'] == 'exa')
                page.locator('#extensions-mcp-search').fill('missing')
                expect(page.locator('#extensions-mcp-list')).to_contain_text('没有匹配')
                page.locator('#extensions-mcp-search').fill('exa')
                expect(page.locator('#extensions-mcp-list .resource-row')).to_have_count(1)
                page.locator('#extensions-mcp-list button', has_text='编辑').click()
                page.locator('#extensions-mcp-form input[name=name]').fill('exa-search')
                page.locator('#extensions-mcp-form button[type=submit]').click()
                expect(page.locator('#extensions-mcp-list .resource-info strong')).to_have_text('exa-search')
                page.locator('[data-settings-tab=workspaces]').click()
                page.locator('#settings-workspace').select_option(first['id'])
                expect(page.locator('#settings-workspace-content')).to_be_visible()
                expect(page.locator('#workspace-model-status')).to_have_text('已读取工作区角色模型。')
                page.locator('#workspace-model-scout').select_option(model['id'])
                page.locator('#workspace-model-form button[type=submit]').click()
                expect(page.locator('#workspace-model-status')).to_have_text('工作区模型已保存。')
                page.locator('#extensions-select-all').click()
                page.locator('#extensions-defaults-form button[type=submit]').click()
                expect(page.locator('#extensions-selection-status')).to_have_text('工作区资源选择已保存。')
                check('workspace model and resources persist', app.role_models(first['id'])['assignments'] == {'scout': model['id']} and len(ext.view(first['id'])['selection']['mcp_ids']) == 1 and len(ext.view(first['id'])['selection']['skill_ids']) == 1)
                page.locator('#settings-workspace').select_option(second['id'])
                expect(page.locator('#workspace-model-status')).to_have_text('已读取工作区角色模型。')
                expect(page.locator('#workspace-model-scout')).to_have_value('__inherit_rule__')
                check('second workspace inherits globals and has no selected resources', page.locator('#extensions-default-options input:checked').count() == 0)
                page.locator('#workspace-model-worker').select_option('')
                page.locator('#settings-workspace').select_option(first['id'])
                expect(page.locator('#confirm-dialog')).to_be_visible()
                page.locator('#confirm-cancel').click()
                expect(page.locator('#settings-workspace')).to_have_value(second['id'])
                check('cancel switching preserves dirty model choice', page.locator('#workspace-model-worker').input_value() == '')
                page.locator('#settings-workspace').select_option(first['id'])
                page.locator('#confirm-accept').click()
                expect(page.locator('#workspace-model-scout')).to_have_value(model['id'])
                page.locator('[data-settings-tab=models]').click()
                page.locator('#model-roles-tab').click()
                page.locator('#role-model-general').select_option(model['id'])
                page.locator('#role-model-form button[type=submit]').click()
                expect(page.locator('#role-model-status')).to_contain_text('已保存')
                page.locator('[data-settings-tab=workspaces]').click()
                expect(page.locator('#workspace-model-general option:checked')).to_contain_text('Analysis model')
                # A global save refreshed the workspace revision, so a subsequent save succeeds.
                page.locator('#workspace-model-form button[type=submit]').click()
                expect(page.locator('#workspace-model-status')).to_have_text('工作区模型已保存。')
                page.locator('[data-settings-tab=models]').click()
                page.locator('#role-model-worker').select_option(model['id'])
                page.locator('[data-settings-tab=workspaces]').click()
                page.locator('#workspace-model-form button[type=submit]').click()
                expect(page.locator('#workspace-model-status')).to_have_text('工作区模型已保存。')
                page.locator('[data-settings-tab=models]').click()
                expect(page.locator('#role-model-worker')).to_have_value(model['id'])
                check('workspace save preserves unsaved global role choices', page.locator('#role-model-status').inner_text() == '有未保存的角色分配。')
                page.locator('[data-settings-tab=workspaces]').click()
                page.locator('#extensions-select-none').click()
                page.locator('#workspace-model-reload').click()
                expect(page.locator('#workspace-model-status')).to_have_text('已读取工作区角色模型。')
                check('model reload preserves unsaved resource choices', page.locator('#extensions-default-options input:checked').count() == 0)
                page.locator('[data-settings-tab=mcp]').click()
                page.locator('#settings-mcp-panel [data-resource-reload]').click()
                page.locator('[data-settings-tab=workspaces]').click()
                check('global library reload preserves unsaved resource choices', page.locator('#extensions-default-options input:checked').count() == 0)
                page.locator('#extensions-reload').click()
                expect(page.locator('#extensions-selection-status')).to_have_text('已读取保存的资源选择。')
                check('resource reload retains saved selections', page.locator('#extensions-default-options input:checked').count() == 2)
                page.locator('[data-settings-tab=mcp]').click()
                expect(page.locator('#extensions-mcp-list .resource-scope')).to_have_text('1 个工作区使用')
                if args.screenshot:
                    page.screenshot(path=str(args.screenshot))
                for width, height in [(390, 844), (768, 1024)]:
                    page.set_viewport_size({'width': width, 'height': height})
                    for tab in ['models', 'mcp', 'skills', 'workspaces']:
                        page.locator(f'[data-settings-tab={tab}]').click()
                        expect(page.locator(f'#settings-{tab}-panel')).to_be_visible()
                        check(f'{width}px {tab} has no horizontal overflow', page.locator('.settings-content').evaluate('e => e.scrollWidth <= e.clientWidth + 1'))
                page.emulate_media(color_scheme='dark')
                page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
                check('settings follows dark mode', page.locator('html').get_attribute('data-theme') == 'dark')
                page.set_viewport_size({'width': 1536, 'height': 1050})
                page.locator('#profiles-dialog > .settings-heading .dialog-close').click()
                page.reload()
                expect(page.locator('#workspace')).to_be_visible()
                page.locator('#profiles-button').click()
                page.locator('[data-settings-tab=workspaces]').click()
                page.locator('#settings-workspace').select_option(first['id'])
                expect(page.locator('#workspace-model-scout')).to_have_value(model['id'])
                check('settings survive browser reload', page.locator('#extensions-default-options input:checked').count() == 2)
                check('no browser exceptions', not errors)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print(f'PASS: {len(checks)} settings browser checks')
    for name in checks:
        print('  ' + name)


if __name__ == '__main__':
    main()
