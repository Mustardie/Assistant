from pathlib import Path

from tools.file_intelligence import (
    FileCategory,
    FileRisk,
    FileSource,
    assess_file_action,
    profile_connector_item,
    profile_file,
    search_file_intent,
)
from tools.contracts import ToolDecisionLayer


def test_profile_schema_and_code_purpose(tmp_path):
    source = tmp_path / "brain" / "runtime.py"
    source.parent.mkdir()
    source.write_text("def think():\n    return 'ok'\n", encoding="utf-8")
    profile = profile_file(source, include_git=False)
    value = profile.to_dict()
    assert profile.category == FileCategory.CODE
    assert "JARVIS brain runtime" in profile.summary.text
    assert value["path"] == str(source.resolve())
    assert value["extension"] == ".py"
    assert value["summary"]["content_inspected"] is True
    assert value["confidence"] >= 0.9
    assert value["evidence"]


def test_log_classification_uses_bounded_content(tmp_path):
    log = tmp_path / ".minecraft" / "logs" / "latest.log"
    log.parent.mkdir(parents=True)
    log.write_text("Fabric Loader failed: Sodium and Iris raised an exception", encoding="utf-8")
    profile = profile_file(log, include_git=False)
    assert profile.category == FileCategory.LOG
    assert "Crash log from" in profile.summary.text
    assert {"minecraft", "crash", "log"} <= set(profile.tags)


def test_log_with_embedded_secret_is_not_treated_as_low_risk(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("startup failed\napi_key=do-not-share\n", encoding="utf-8")
    profile = profile_file(log, include_git=False)
    assert profile.risk == FileRisk.CRITICAL
    assert "do not share or commit" in profile.summary.text


def test_media_summaries_are_honest_metadata_inferences(tmp_path):
    image = tmp_path / "ui_reference_mockup.png"
    video = tmp_path / "minecraft_replay.mp4"
    image.write_bytes(b"not-a-real-image")
    video.write_bytes(b"not-a-real-video")
    image_profile = profile_file(image, include_git=False)
    video_profile = profile_file(video, include_git=False)
    assert image_profile.category == FileCategory.IMAGE
    assert "inferred from metadata" in image_profile.summary.text
    assert image_profile.summary.content_inspected is False
    assert video_profile.category == FileCategory.VIDEO
    assert "Minecraft replay" in video_profile.summary.text


def test_secrets_models_settings_and_build_outputs_are_protected(tmp_path):
    secret = tmp_path / ".env"
    model = tmp_path / "models" / "assistant.gguf"
    settings = tmp_path / "user_settings.json"
    generated = tmp_path / "build" / "module.pyc"
    model.parent.mkdir()
    generated.parent.mkdir()
    secret.write_text("API_KEY=secret", encoding="utf-8")
    model.write_bytes(b"weights")
    settings.write_text('{"theme": "dark"}', encoding="utf-8")
    generated.write_bytes(b"generated")

    secret_profile = profile_file(secret, include_git=False)
    model_profile = profile_file(model, include_git=False)
    settings_profile = profile_file(settings, include_git=False)
    build_profile = profile_file(generated, include_git=False)
    assert secret_profile.risk == FileRisk.CRITICAL
    assert model_profile.category == FileCategory.LOCAL_MODEL
    assert model_profile.risk == FileRisk.HIGH
    assert settings_profile.category == FileCategory.SETTINGS
    assert build_profile.category == FileCategory.BUILD_ARTIFACT
    assert build_profile.risk == FileRisk.LOW
    assert not assess_file_action(secret, "share")["allowed_without_confirmation"]


def test_intent_search_ranks_purpose_not_only_filename(tmp_path):
    minecraft = tmp_path / "game" / "logs" / "latest.log"
    unrelated = tmp_path / "notes" / "latest.txt"
    minecraft.parent.mkdir(parents=True)
    unrelated.parent.mkdir(parents=True)
    minecraft.write_text("Minecraft Fabric crash exception from Sodium", encoding="utf-8")
    unrelated.write_text("shopping list", encoding="utf-8")
    records = [
        {"path": str(unrelated), "filename": unrelated.name, "summary": "shopping list"},
        {"path": str(minecraft), "filename": minecraft.name, "summary": "Fabric Sodium error"},
    ]
    results = search_file_intent("find the Minecraft crash log", records, limit=2)
    assert results[0]["path"] == str(minecraft.resolve())
    assert "Crash log" in results[0]["summary"]
    assert results[0]["evidence"]


def test_connector_attachment_becomes_a_file_profile():
    profile = profile_connector_item(
        {"filename": "Class_10_Hindi_Worksheet.pdf", "size": 2048, "mime_type": "application/pdf", "id": "drive-1"},
        source=FileSource.GOOGLE_DRIVE,
    )
    assert profile.source == FileSource.GOOGLE_DRIVE
    assert profile.path == "drive-1"
    assert profile.category == FileCategory.DOCUMENT
    assert "Hindi" in profile.summary.text


def test_tool_decision_requires_confirmation_to_move_important_source(tmp_path):
    source = tmp_path / "brain" / "agent.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    layer = ToolDecisionLayer({"file_move": lambda source, destination, confirm=False: None})
    blocked = layer.assess("file_move", {"source": str(source), "destination": str(tmp_path / "elsewhere.py")})
    confirmed = layer.assess(
        "file_move",
        {"source": str(source), "destination": str(tmp_path / "elsewhere.py")},
        confirmed=True,
    )
    assert not blocked.allowed
    assert blocked.requires_confirmation
    assert confirmed.allowed
