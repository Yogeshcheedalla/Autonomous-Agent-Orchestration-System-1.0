import { readFile, stat } from 'fs/promises';
import path from 'path';
import { NextResponse } from 'next/server';

const GENERATED_ROOT = path.resolve(process.cwd(), 'generated_artifacts');

const CONTENT_TYPES: Record<string, string> = {
  '.csv': 'text/csv; charset=utf-8',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.html': 'text/html; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.json': 'application/json; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
  '.pdf': 'application/pdf',
  '.png': 'image/png',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.zip': 'application/zip',
};

function safeGeneratedPath(parts: string[]) {
  const decodedParts = parts.map((part) => decodeURIComponent(part));
  const requestedPath = path.resolve(GENERATED_ROOT, ...decodedParts);
  const relativePath = path.relative(GENERATED_ROOT, requestedPath);

  if (relativePath.startsWith('..') || path.isAbsolute(relativePath)) {
    return null;
  }

  return requestedPath;
}

export async function GET(_request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path: routePath } = await context.params;
  const filePath = safeGeneratedPath(routePath || []);

  if (!filePath) {
    return new NextResponse('Invalid generated file path.', { status: 400 });
  }

  try {
    const fileStat = await stat(filePath);
    if (!fileStat.isFile()) {
      return new NextResponse('Generated file not found.', { status: 404 });
    }

    const body = await readFile(filePath);
    const extension = path.extname(filePath).toLowerCase();
    const filename = path.basename(filePath);

    return new NextResponse(body, {
      headers: {
        'Content-Disposition': `inline; filename="${filename.replace(/"/g, '')}"`,
        'Content-Length': String(fileStat.size),
        'Content-Type': CONTENT_TYPES[extension] || 'application/octet-stream',
        'Cache-Control': 'no-store',
      },
    });
  } catch {
    return new NextResponse('Generated file not found.', { status: 404 });
  }
}
