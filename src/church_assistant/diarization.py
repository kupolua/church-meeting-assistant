"""Speaker diarization за допомогою pyannote-audio."""

import argparse
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pyannote.audio import Pipeline


def run_diarization(audio_path: Path) -> Any:
    """Запустити diarization на аудіо файлі.

    Args:
        audio_path: Шлях до аудіо файлу (m4a, wav, etc)

    Returns:
        pyannote Annotation object з speaker turns
    """
    load_dotenv()
    token = os.getenv("HUGGINGFACE_TOKEN")
    if not token:
        raise ValueError("HUGGINGFACE_TOKEN не встановлений у .env")

    print("Завантажуємо pyannote-audio pipeline...")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=token,
    )

    print(f"Прогоняємо diarization на {audio_path.name}...")
    start = time.time()

    # Це **повільний** виклик — може зайняти 1-2 години на M1 CPU
    output = pipeline(str(audio_path))

    elapsed = time.time() - start
    print(f"\nЧас обробки: {elapsed:.1f}s ({elapsed/60:.1f} хв)")

    # pyannote-audio 4.x повертає DiarizeOutput, а не Annotation напряму.
    # Дістаємо саме Annotation (на ньому є .itertracks() / .write_rttm()).
    return getattr(output, "speaker_diarization", output)


def save_diarization(diarization: Any, rttm_path: Path) -> None:
    """Зберегти diarization у RTTM форматі."""
    rttm_path.parent.mkdir(parents=True, exist_ok=True)
    with open(rttm_path, "w") as f:
        diarization.write_rttm(f)
    print(f"Збережено в RTTM форматі: {rttm_path}")


def load_diarization(rttm_path: Path) -> Any:
    """Завантажити раніше збережений diarization з RTTM (секунди замість годин)."""
    from pyannote.database.util import load_rttm

    annotations = load_rttm(str(rttm_path))
    if not annotations:
        raise ValueError(f"RTTM файл порожній або некоректний: {rttm_path}")
    # load_rttm повертає {uri: Annotation}; беремо перший (зазвичай єдиний).
    return next(iter(annotations.values()))


def get_diarization(audio_path: Path, rttm_path: Path, *, use_cache: bool = True) -> Any:
    """Повернути diarization: з кешу (RTTM), якщо є, інакше — порахувати й зберегти."""
    if use_cache and rttm_path.exists():
        print(f"Завантажуємо diarization з кешу: {rttm_path}")
        return load_diarization(rttm_path)

    diarization = run_diarization(audio_path)
    # СПЕРШУ зберігаємо — обчислення коштує ~2 години, тож збій нижче не знищить результат.
    save_diarization(diarization, rttm_path)
    return diarization


def print_diarization_summary(diarization: Any) -> None:
    """Вивести сумарну інформацію про diarization."""
    speakers = set()
    total_speech_time = 0.0

    for turn, _, speaker in diarization.itertracks(yield_label=True):
        speakers.add(speaker)
        total_speech_time += turn.end - turn.start

    print(f"\n{'=' * 60}")
    print(f"Кількість унікальних speakers: {len(speakers)}")
    print(f"Список: {sorted(speakers)}")
    print(f"Загальний час мовлення: {total_speech_time:.1f}s ({total_speech_time/60:.1f} хв)")
    print(f"{'=' * 60}")


def print_diarization_segments(diarization: Any, limit: int = 20) -> None:
    """Вивести перші N сегментів для перевірки."""
    print(f"\nПерші {limit} сегментів:")
    for i, (turn, _, speaker) in enumerate(diarization.itertracks(yield_label=True)):
        if i >= limit:
            break
        print(
            f"  [{turn.start:7.2f}s -> {turn.end:7.2f}s] "
            f"{speaker} ({turn.end - turn.start:.1f}s)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Speaker diarization для аудіо запису зустрічі."
    )
    parser.add_argument(
        "audio",
        nargs="?",
        default="data/test_baseline.m4a",
        help="Шлях до аудіо файлу (за замовчуванням: data/test_baseline.m4a)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Шлях для RTTM (за замовчуванням: <audio>.rttm поряд з аудіо)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ігнорувати наявний RTTM і пораховати diarization заново",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Скільки сегментів вивести для перевірки (за замовчуванням: 20)",
    )
    args = parser.parse_args()

    audio_path = Path(args.audio)
    rttm_path = Path(args.output) if args.output else audio_path.with_suffix(".rttm")

    diarization = get_diarization(audio_path, rttm_path, use_cache=not args.no_cache)

    print_diarization_summary(diarization)
    print_diarization_segments(diarization, limit=args.limit)


if __name__ == "__main__":
    main()