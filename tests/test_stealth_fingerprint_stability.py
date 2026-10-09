from scraper.stealth_js import AUDIO_SPOOF_JS, CANVAS_SPOOF_JS, NAVIGATOR_SPOOF_JS


def test_canvas_spoof_is_stable_and_does_not_mutate_source_canvas():
    assert "function copyWithNoise(data)" in CANVAS_SPOOF_JS
    assert "new Uint8ClampedArray(data)" in CANVAS_SPOOF_JS
    assert "document.createElement('canvas')" in CANVAS_SPOOF_JS
    assert "ctx.putImageData(imageData, 0, 0)" not in CANVAS_SPOOF_JS


def test_audio_spoof_does_not_redraw_random_noise_on_each_read():
    assert "AUDIO_NOISE * ((i % 7) - 3)" in AUDIO_SPOOF_JS
    assert "((i % 3) - 1)" in AUDIO_SPOOF_JS
    assert "AUDIO_NOISE * (Math.random() - 0.5)" not in AUDIO_SPOOF_JS


def test_navigator_values_are_stable_within_the_session():
    assert "const DOWNLINK =" in NAVIGATOR_SPOOF_JS
    assert "get: () => DOWNLINK" in NAVIGATOR_SPOOF_JS
    assert "const BATTERY_LEVEL =" in NAVIGATOR_SPOOF_JS
    assert "level: BATTERY_LEVEL" in NAVIGATOR_SPOOF_JS
