'use strict';

// The worker and language data are installed within this project. No document
// is uploaded: recognition runs entirely in the local WebAssembly worker.
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const image = path.resolve(process.argv[2] || '');
const relative = path.relative(root, image);
if (!relative || relative.startsWith('..') || path.isAbsolute(relative) || !fs.existsSync(image)) {
  process.stderr.write('OCR input must be an existing image inside the project.\n');
  process.exit(2);
}
const { createWorker } = require(path.join(root, '.local', 'ocr', 'node_modules', 'tesseract.js'));
const dataPath = path.join(root, '.local', 'ocr', 'tessdata');
fs.mkdirSync(dataPath, { recursive: true });

(async () => {
  let worker;
  try {
    worker = await createWorker(['kor', 'eng'], 1, {
      langPath: dataPath,
      cachePath: dataPath,
      cacheMethod: 'write',
      gzip: true,
      logger: () => {},
    });
    await worker.setParameters({ user_defined_dpi: '300', preserve_interword_spaces: '1' });
    const result = await worker.recognize(image, {}, { tsv: true, text: false });
    if (typeof result.data.tsv !== 'string') throw new Error('OCR did not return TSV.');
    // Tesseract.js GetTSVText omits the column header emitted by the native CLI.
    process.stdout.write('level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n');
    process.stdout.write(result.data.tsv);
  } finally {
    if (worker) await worker.terminate();
  }
})().catch(error => {
  process.stderr.write(String(error.message).slice(0, 600) + '\n');
  process.exitCode = 1;
});
