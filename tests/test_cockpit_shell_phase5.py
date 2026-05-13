from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
PANELS = (ROOT / 'static' / 'panels.js').read_text(encoding='utf-8')
STYLE = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')
ROUTES = (ROOT / 'api' / 'routes.py').read_text(encoding='utf-8')


def test_cockpit_nav_and_main_surfaces_exist():
    for panel in ['paperclip', 'reports', 'controlPlane', 'sessionCleanup']:
        assert f'data-panel="{panel}"' in INDEX
    for node in ['panelPaperclip', 'panelReports', 'panelControlPlane', 'panelSessionCleanup', 'mainPaperclip', 'mainReports', 'mainControlPlane', 'mainSessionCleanup']:
        assert f'id="{node}"' in INDEX


def test_cockpit_copy_preserves_korean_boundaries():
    for text in ['읽기 전용 조직 상태', '기존 Paperclip 이슈에 댓글만 추가합니다.', '오늘의 요약', 'Hermes 처리 판단', 'Telegram은 짧은 알림만', '가역 quarantine 우선']:
        assert text in INDEX or text in PANELS


def test_cockpit_loader_functions_and_routes():
    for fn in ['loadPaperclipCockpit', 'loadReportsCockpit', 'loadControlPlane', 'loadSessionCleanup']:
        assert f'function {fn}' in PANELS or f'async function {fn}' in PANELS
    for route in ['/api/paperclip/status', '/api/reports/morning-brief/latest', '/api/control-plane/overview', '/api/sessions/cleanup_report']:
        assert route in PANELS
        assert route in ROUTES


def test_cockpit_main_view_switching_registered():
    for cls in ['showing-paperclip', 'showing-reports', 'showing-controlPlane', 'showing-sessionCleanup']:
        assert cls in STYLE
        assert cls in PANELS
    assert "path==='/control-plane'" in PANELS
    assert "path==='/reports'||path==='/reports/morning-brief'" in PANELS
