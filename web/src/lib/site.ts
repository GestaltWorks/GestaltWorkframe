import type { PublicDeploymentConfig } from "./deploymentConfig";

export const siteUrl = (process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000").replace(/\/$/, "");
export const socialImageUrl = process.env.NEXT_PUBLIC_SOCIAL_IMAGE_URL ?? `${siteUrl}/brand/social-card.png`;

export const siteUrlFromDeploymentConfig = (config: PublicDeploymentConfig): string => config.site.base_url.replace(/\/$/, "");