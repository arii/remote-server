// Transpile-only syntax gate driver (issue #488). Runs one file
// through the bundled TypeScript compiler's transpileModule, which
// reports syntactic errors without module resolution or type
// checking — exactly the AST gate's job (files validate standalone,
// outside node_modules, so `tsc --noEmit` would false-fail on
// unresolved imports).
//
// Usage: node ts_check.js <file> ; prints {"errors": [{line, message}]}
// The typescript lib directory comes from TS_LIB_DIR (worker image:
// /opt/tscheck/lib; dev: <repo>/frontend/node_modules/typescript/lib).
'use strict';
const fs = require('fs');
const path = require('path');

const libDir = process.env.TS_LIB_DIR;
if (!libDir) {
  console.log(JSON.stringify({ errors: [], fatal: 'TS_LIB_DIR unset' }));
  process.exit(0);
}
let ts;
try {
  ts = require(path.join(libDir, 'typescript.js'));
} catch (err) {
  console.log(
    JSON.stringify({ errors: [], fatal: `typescript load failed: ${err.message}` }),
  );
  process.exit(0);
}
const file = process.argv[2];
const code = fs.readFileSync(file, 'utf8');
const result = ts.transpileModule(code, {
  compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX,
    target: ts.ScriptTarget.ESNext,
    allowJs: true,
  },
  reportDiagnostics: true,
  fileName: file,
});
const errors = (result.diagnostics || [])
  .filter((d) => d.category === ts.DiagnosticCategory.Error)
  .map((d) => {
    let line = null;
    if (d.file && typeof d.start === 'number') {
      line = d.file.getLineAndCharacterOfPosition(d.start).line + 1;
    }
    return {
      line,
      message: ts.flattenDiagnosticMessageText(d.messageText, ' ').slice(0, 300),
    };
  });
console.log(JSON.stringify({ errors }));
