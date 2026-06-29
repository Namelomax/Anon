"""Тесты детерминированного фильтра шума (is_noise_span) и тайм-кодов."""
from anonymizer.detectors import is_noise_span, is_timecode


def test_timecodes_dropped_for_any_label():
    for label in ("SNILS", "INN", "LOCATION", "PERSON", "ORG"):
        assert is_noise_span("00:20:08", label) is True
        assert is_noise_span("00:00:28", label) is True
        assert is_noise_span("3:05", label) is True
    assert is_timecode("01:10:19") is True
    assert is_timecode("Иванов") is False


def test_speaker_labels_dropped():
    for v in ("Спикер 4", "Спикер 1", "Speaker 2", "Участник 3"):
        assert is_noise_span(v, "PERSON") is True


def test_common_words_and_phrases_dropped():
    drop_person = [
        "человек", "ассистент", "пользователем", "ведущий",
        "это всё равно", "подключается всё равно", "там условно",
        "день может непрерывной", "Так-так-так",
    ]
    for v in drop_person:
        assert is_noise_span(v, "PERSON") is True, v
    drop_org = [
        "Вот.", "У нас", "заказчиком", "компании", "команда", "Нейросеть",
        "искусственный интеллект", "Решение искусственному интеллекту",
        "проектной команды",
    ]
    for v in drop_org:
        assert is_noise_span(v, "ORG") is True, v


def test_real_entities_kept():
    keep = [
        ("Никита Касьянов", "PERSON"), ("Никита Грицанюк", "PERSON"),
        ("Екатерина", "PERSON"), ("Иванов И.И.", "PERSON"),
        ("Командиров", "PERSON"),  # реальная фамилия с основой «команд» — не резать
        ("Форус", "ORG"), ("Dream Team", "ORG"), ("КФК", "ORG"),
        ("Оптима Капитан Яков", "ORG"), ("Москва", "LOCATION"),
        ("ivan@mail.ru", "EMAIL"), ("7707083893", "INN"),
    ]
    for v, label in keep:
        assert is_noise_span(v, label) is False, v
