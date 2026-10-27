/** @type {import('next').NextConfig} */
const nextConfig = {
  // Allow larger uploads to pass through the proxy API route (docx files).
  experimental: {
    serverActions: { bodySizeLimit: "25mb" },
    // Сервер сборки — с небольшой памятью и без подкачки на диск (угроза У-5
    // модели угроз): параллельные воркеры сборки съедали память до SIGKILL.
    // Один воркер и экономный режим webpack — дольше, но укладывается.
    cpus: 1,
    webpackMemoryOptimizations: true,
  },
};

export default nextConfig;
