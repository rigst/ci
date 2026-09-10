// Configuração compartilhada de JavaScript dos projetos do rigst.
//
// Por que existe: são 6.654 linhas de JS escrito à mão na frota — 2.104 no
// sistema_vetorial, 1.939 no divisor_pdf — e até aqui nenhuma verificação
// passava por elas. O planejamento original dedicava onze subseções ao CSS e
// nenhuma ao JavaScript, embora o JS carregue lógica de negócio (OCR, upload,
// máscara de formulário) e o CSS não.
//
// Não estende eslint:recommended inteiro: o preset traz regras de estilo e
// casos que não se aplicam a script clássico servido pelo Django. As regras
// abaixo pegam defeito, não gosto — variável que não existe, comparação frouxa,
// atribuição dentro de condição, caso de switch que vaza para o seguinte.
//
// sourceType "script" e não "module": estes arquivos são carregados por
// <script src> direto do template, sem bundler. Nenhum projeto da frota tem
// package.json de build. Declarar "module" faria o parser aceitar import/export
// que o navegador rejeitaria em runtime — o oposto do que um lint deve fazer.
//
// Os globais do navegador vêm do pacote `globals`, e não de uma lista escrita à
// mão. A primeira versão desta config listava os globais manualmente, para
// poupar uma dependência — e a medição na frota devolveu sete `no-undef`, dos
// quais cinco eram globais legítimos que a lista tinha esquecido (EventSource,
// NodeFilter, DataTransfer, FontFace, HTMLFormElement). Lista curada à mão de
// uma API que cresce todo ano produz falso positivo por construção, e falso
// positivo em regra de erro é o que faz alguém desligar o job.
//
// À mão ficam só as bibliotecas que os templates carregam por <script>: essas
// nenhum pacote conhece.

import globals from "globals";

const BIBLIOTECAS = {
  htmx: "readonly",
  Chart: "readonly",
  fabric: "readonly",
  pdfjsLib: "readonly",
  Tesseract: "readonly",
  // `module` entra porque o padrão UMD guardado por
  // `typeof module !== 'undefined' && module.exports` é usado de propósito no
  // sistema_questoes, para o mesmo parser rodar sob Node num teste. O no-undef
  // acusa a segunda referência da linha, não a guarda — e sem bundler em
  // nenhum projeto da frota não há uso acidental de CommonJS a proteger.
  module: "readonly",
};

export default [
  {
    files: ["**/*.js"],
    ignores: [
      "**/*.min.js",
      "**/staticfiles/**",
      "**/node_modules/**",
      "**/venv/**",
      "**/.venv/**",
      "**/static/**/vendor/**",
    ],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...BIBLIOTECAS },
    },
    linterOptions: {
      // Comentário de desativação que não desativa nada é resto de um conserto
      // já feito. Avisar mantém o arquivo honesto sem derrubar o build.
      reportUnusedDisableDirectives: "warn",
    },
    rules: {
      // -- defeito de verdade -------------------------------------------
      "no-undef": "error",
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-duplicate-case": "error",
      "no-unreachable": "error",
      "no-func-assign": "error",
      "no-obj-calls": "error",
      "no-sparse-arrays": "error",
      "use-isnan": "error",
      "valid-typeof": "error",
      "no-self-assign": "error",
      "no-self-compare": "error",
      "no-unsafe-negation": "error",
      "no-unsafe-finally": "error",
      // "except-parens" e não "always": `while ((m = re.exec(s)) !== null)` é a
      // forma idiomática de percorrer um regex global, e os parênteses extras já
      // são a declaração de intenção que a regra pede. Com "always", as três
      // únicas ocorrências da frota eram todas esse laço — nenhum defeito.
      "no-cond-assign": ["error", "except-parens"],
      "no-fallthrough": "error",
      "no-const-assign": "error",
      "no-class-assign": "error",
      "no-compare-neg-zero": "error",

      // -- fonte clássica de bug silencioso ------------------------------
      "eqeqeq": ["error", "smart"],
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-script-url": "error",

      // -- resíduo de remendo, que é o tema deste pipeline ----------------
      "no-unused-vars": ["warn", { args: "none", varsIgnorePattern: "^_" }],
      "no-empty": ["warn", { allowEmptyCatch: false }],
      "no-console": ["warn", { allow: ["warn", "error"] }],
    },
  },
];
