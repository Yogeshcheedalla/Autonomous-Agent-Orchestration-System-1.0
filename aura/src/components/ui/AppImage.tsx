'use client';

import React, { useState, useCallback, useMemo, memo } from 'react';
import Image, { type ImageProps } from 'next/image';

/**
 * Built on next/image's own props rather than a hand-written list plus
 * `[key: string]: any`.
 *
 * The index signature existed so callers could pass anything straight through to
 * `<Image>` -- and the only caller in the tree, AppLogo, relies on it for `style`,
 * which the hand-written list forgot. But `any` meant the passthrough accepted
 * misspelled and invented props with equal enthusiasm and forwarded them to the
 * DOM. Deriving from `ImageProps` keeps the passthrough open to every prop
 * next/image really has, and closed to the ones it does not.
 *
 * `src` is narrowed from `string | StaticImport` to `string` because this
 * component calls `.startsWith('http')` on it, and `fallbackSrc` is a string path.
 */
type AppImageProps = Omit<ImageProps, 'src' | 'onError' | 'onLoad'> & {
  src: string;
  fallbackSrc?: string;
};

const AppImage = memo(function AppImage({
  src,
  alt,
  width,
  height,
  className = '',
  priority = false,
  quality = 85,
  placeholder = 'empty',
  blurDataURL,
  fill = false,
  sizes,
  onClick,
  fallbackSrc = '/assets/images/no_image.png',
  loading = 'lazy',
  unoptimized = false,
  ...props
}: AppImageProps) {
  const [imageSrc, setImageSrc] = useState(src);
  const [isLoading, setIsLoading] = useState(true);
  const [hasError, setHasError] = useState(false);

  const isExternalUrl = useMemo(
    () => typeof imageSrc === 'string' && imageSrc.startsWith('http'),
    [imageSrc]
  );
  const resolvedUnoptimized = unoptimized || isExternalUrl;

  const handleError = useCallback(() => {
    if (!hasError && imageSrc !== fallbackSrc) {
      setImageSrc(fallbackSrc);
      setHasError(true);
    }
    setIsLoading(false);
  }, [hasError, imageSrc, fallbackSrc]);

  const handleLoad = useCallback(() => {
    setIsLoading(false);
    setHasError(false);
  }, []);

  const imageClassName = useMemo(() => {
    const classes = [className];
    if (isLoading) classes.push('bg-gray-200');
    if (onClick) classes.push('cursor-pointer hover:opacity-90 transition-opacity duration-200');
    return classes.filter(Boolean).join(' ');
  }, [className, isLoading, onClick]);

  const imageProps = useMemo(() => {
    const baseProps: Partial<ImageProps> & { src: string; alt: string } = {
      src: imageSrc,
      alt,
      className: imageClassName,
      quality,
      placeholder,
      unoptimized: resolvedUnoptimized,
      onError: handleError,
      onLoad: handleLoad,
      onClick,
    };

    if (priority) {
      baseProps.priority = true;
    } else {
      baseProps.loading = loading;
    }

    if (blurDataURL && placeholder === 'blur') {
      baseProps.blurDataURL = blurDataURL;
    }

    return baseProps;
  }, [
    imageSrc,
    alt,
    imageClassName,
    quality,
    placeholder,
    blurDataURL,
    resolvedUnoptimized,
    priority,
    loading,
    handleError,
    handleLoad,
    onClick,
  ]);

  // `alt` is already inside `imageProps`, but it is passed again explicitly
  // below. Two reasons: the a11y lint rule cannot see an alt that arrives
  // through a spread of a memoised object, and `{...props}` spreads *after*
  // `imageProps`, so an explicit `alt` last is what guarantees the caller's
  // required alt text always wins.
  if (fill) {
    return (
      <div className="relative" style={{ width: '100%', height: '100%' }}>
        <Image
          {...imageProps}
          fill
          sizes={sizes || '(max-width: 768px) 100vw, (max-width: 1200px) 50vw, 33vw'}
          style={{ objectFit: 'cover' }}
          {...props}
          alt={alt}
        />
      </div>
    );
  }

  return (
    <Image
      {...imageProps}
      width={width || 400}
      height={height || 300}
      sizes={sizes}
      {...props}
      alt={alt}
    />
  );
});

AppImage.displayName = 'AppImage';

export default AppImage;
