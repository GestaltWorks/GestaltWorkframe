import { apiUrl } from "./api";

export type DeploymentNavItem = {
  label: string;
  href: string;
};

export type DeploymentIntakeQuestion = {
  id: string;
  label: string;
  options: string[];
};

export type PublicDeploymentConfig = {
  deployment_id: string;
  brand: {
    name: string;
    colors: Record<string, string>;
    fonts: Record<string, string>;
    logo_path: string;
  };
  identity: {
    organization_name: string;
    short_name: string;
    public_email: string;
    bot_name: string;
  };
  site: {
    base_url: string;
    title: string;
    description: string;
  };
  nav: DeploymentNavItem[];
  copy: Record<string, unknown>;
  intake: DeploymentIntakeQuestion[];
  newsletter: {
    enabled: boolean;
    name: string;
    cadence_days: number;
  };
  discovery: { enabled: boolean };
  curriculum: { enabled: boolean };
};

export async function fetchDeploymentConfig(fetcher: typeof fetch = fetch): Promise<PublicDeploymentConfig> {
  const response = await fetcher(apiUrl("/api/deployment-config"), { cache: "force-cache" });
  if (!response.ok) {
    throw new Error(`Deployment config request failed: ${response.status}`);
  }
  return (await response.json()) as PublicDeploymentConfig;
}
