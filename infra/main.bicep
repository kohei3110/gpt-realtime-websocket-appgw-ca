@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Resource name prefix. Keep it short and unique per environment.')
@minLength(3)
param prefix string = 'gptrt'

@description('Initial container image to deploy. Replace with your Azure Container Registry image when ready.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Container app target port exposed by FastAPI.')
param containerTargetPort int = 8080

@description('Minimum replica count for the container app.')
param minReplicas int = 1

@description('Maximum replica count for the container app.')
param maxReplicas int = 2

@description('Azure Container Registry SKU.')
@allowed(['Basic', 'Standard', 'Premium'])
param containerRegistrySku string = 'Basic'

@description('Azure OpenAI SKU name.')
@allowed(['S0'])
param openAiSkuName string = 'S0'

@description('Azure OpenAI deployment name for the realtime model.')
param openAiDeploymentName string = 'gpt-realtime'

@description('Azure OpenAI model name to deploy.')
param openAiModelName string = 'gpt-realtime'

@description('Azure OpenAI model version to deploy.')
param openAiModelVersion string = '2025-08-28'

@description('Azure OpenAI deployment SKU (GlobalStandard recommended for realtime).')
@allowed(['GlobalStandard'])
param openAiDeploymentSku string = 'GlobalStandard'

@description('Azure OpenAI deployment capacity (tokens per minute).')
param openAiDeploymentCapacity int = 10


@secure()
@description('Optional Azure OpenAI API key to preload as a secret in the container app.')
param openAiApiKey string = ''

var tags = {
  environment: 'dev'
  workload: 'gpt-realtime-websocket'
}

var acrName = toLower('${prefix}acr')
var openAiAccountName = toLower('${prefix}aoai')
var openAiSecretProvided = !empty(openAiApiKey)

resource acr 'Microsoft.ContainerRegistry/registries@2023-01-01-preview' = {
  name: acrName
  location: location
  sku: {
    name: containerRegistrySku
  }
  properties: {
    adminUserEnabled: true
  }
  tags: tags
}

var acrCredentials = listCredentials(acr.id, '2019-05-01')

resource openAiAccount 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: openAiAccountName
  location: location
  sku: {
    name: openAiSkuName
  }
  kind: 'OpenAI'
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: '${prefix}aoai'
  }
  tags: tags
}

resource openAiDeployment 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  name: openAiDeploymentName
  parent: openAiAccount
  properties: {
    model: {
      format: 'OpenAI'
      name: openAiModelName
      version: openAiModelVersion
    }
    raiPolicyName: 'Microsoft.Default'
  }
  sku: {
    name: openAiDeploymentSku
    capacity: openAiDeploymentCapacity
  }
}

module containerApp './modules/containerApp.bicep' = {
  name: 'containerAppModule'
  params: {
    prefix: prefix
    location: location
    containerImage: containerImage
    targetPort: containerTargetPort
    minReplicas: minReplicas
    maxReplicas: maxReplicas
    registryServer: acr.properties.loginServer
    registryUsername: acrCredentials.username
    registryPassword: acrCredentials.passwords[0].value
    openAiEndpoint: openAiAccount.properties.endpoint
    openAiDeploymentName: openAiDeploymentName
    openAiApiKey: openAiApiKey
    includeOpenAiSecret: openAiSecretProvided
  }
}

module applicationGateway './modules/applicationGateway.bicep' = {
  name: 'applicationGatewayModule'
  params: {
    prefix: prefix
    location: location
    backendFqdn: containerApp.outputs.ingressFqdn
    backendPort: 443
  }
}

output containerAppUrl string = containerApp.outputs.ingressFqdn
output containerAppName string = containerApp.outputs.containerAppName
output applicationGatewayPublicIp string = applicationGateway.outputs.publicIpAddress
output azureOpenAiEndpoint string = openAiAccount.properties.endpoint
output azureOpenAiDeployment string = openAiDeploymentName
output containerRegistryLoginServer string = acr.properties.loginServer
