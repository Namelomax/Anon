/**
 * Выброс картинок из .docx перед отправкой на сервер.
 *
 * Зачем: анонимизатор работает с ТЕКСТОМ — распознавания картинок в конвейере
 * нет, так что вложенные изображения не влияют на результат вообще никак. Зато
 * они полностью определяют вес файла и упираются в лимит запроса платформы
 * (~4.5 МБ). Замер на тестовых документах: 19.93 МБ -> 42 КБ, 6.43 МБ -> 38 КБ,
 * 3.56 МБ -> 38 КБ. То есть после чистки в лимит проходит практически что
 * угодно.
 *
 * Как: .docx — это zip. Файлы в `word/media/` НЕ удаляются, а подменяются
 * прозрачным PNG размером 1×1. Удаление сломало бы связи в `word/_rels/` и
 * разметку `<w:drawing>`, и Word ругался бы на повреждённый документ; подмена
 * содержимого оставляет структуру валидной, а вес обнуляет.
 *
 * Расплата честная и её видно в интерфейсе: в скачанном обезличенном документе
 * на месте картинок будут пустые рамки. Для расшифровок совещаний это обычно
 * приемлемо, для договоров со сканами подписей — нет, поэтому переключатель
 * оставлен пользователю.
 */
import JSZip from "jszip";

/** Прозрачный PNG 1×1 — минимально возможная валидная картинка. */
const BLANK_PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

function blankPngBytes(): Uint8Array {
  const bin = atob(BLANK_PNG_BASE64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export type StripResult = {
  file: File;
  /** Сколько изображений подменено. 0 — файл вернулся нетронутым. */
  removed: number;
  bytesBefore: number;
  bytesAfter: number;
};

/**
 * Вернуть копию .docx без веса картинок. Для не-.docx и при любой ошибке
 * разбора возвращает исходный файл нетронутым: задача — уменьшить размер, а не
 * рискнуть документом.
 */
export async function stripDocxMedia(file: File): Promise<StripResult> {
  const bytesBefore = file.size;
  const unchanged: StripResult = { file, removed: 0, bytesBefore, bytesAfter: bytesBefore };

  if (!file.name.toLowerCase().endsWith(".docx")) return unchanged;

  try {
    const zip = await JSZip.loadAsync(await file.arrayBuffer());
    const media = Object.keys(zip.files).filter(
      (n) => n.startsWith("word/media/") && !zip.files[n].dir,
    );
    if (media.length === 0) return unchanged;

    const stub = blankPngBytes();
    for (const name of media) zip.file(name, stub);

    const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
    const stripped = new File([blob], file.name, { type: file.type });
    return {
      file: stripped,
      removed: media.length,
      bytesBefore,
      bytesAfter: stripped.size,
    };
  } catch (e) {
    console.warn("[docx] не удалось вычистить картинки, отправляю как есть:", e);
    return unchanged;
  }
}
