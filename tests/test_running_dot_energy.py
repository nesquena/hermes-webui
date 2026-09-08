"""Keep the activity pulse cheap without suppressing its running state."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_running_dot_pulse_does_not_animate_paint_properties():
    css = (ROOT / 'static/style.css').read_text()
    pulse = css.split('@keyframes wlpulse{', 1)[1].split('\n', 1)[0]
    assert 'opacity:' in pulse
    assert 'box-shadow:' not in pulse
    assert 'filter:' not in pulse


def test_running_dot_honors_reduced_motion():
    css = (ROOT / 'static/style.css').read_text()
    assert '@media (prefers-reduced-motion:reduce){\n  .tool-card-running-dot,.tl-rundot{animation:none;}' in css
