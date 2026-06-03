#!/usr/bin/env node

import * as esbuild from 'esbuild';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

/** @type {import('esbuild').BuildOptions} */
const buildOptions = {
  entryPoints: [join(__dirname, '../src/index.ts')],
  bundle: true,
  platform: 'node',
  target: 'node18',
  outfile: join(__dirname, '../dist/index.js'),
  format: 'esm',
  packages: 'external', // Don't bundle node_modules
  sourcemap: false,
};

try {
  await esbuild.build(buildOptions);
  process.stderr.write('Build completed successfully\n');
} catch (error) {
  process.stderr.write(`Build failed: ${error.message}\n`);
  process.exit(1);
}
