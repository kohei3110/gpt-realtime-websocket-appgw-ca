@description('Resource name prefix for managed environment and container app.')
@minLength(3)
param prefix string

@description('Azure region for the resources.')
param location string

@description('Container image to deploy.')
param containerImage string

@description('Container port exposed through ingress.')
param targetPort int

@description('Minimum replica count.')
param minReplicas int = 1

@description('Maximum replica count.')
param maxReplicas int = 2

@description('Azure Container Registry server, e.g. contoso.azurecr.io.')
param registryServer string

@description('Azure Container Registry username.')
param registryUsername string

@secure()
@description('Azure Container Registry password.')
param registryPassword string

@description('Grace period (seconds) between SIGTERM and SIGKILL.')
param terminationGracePeriodSeconds int = 30

@description('Azure OpenAI endpoint, e.g. https://example.openai.azure.com/')
param openAiEndpoint string

@description('Azure OpenAI deployment name for realtime model.')
param openAiDeploymentName string

@secure()
@description('Azure OpenAI API key secret value. Leave empty to supply later.')
param openAiApiKey string = ''

@description('Set to true to create the Azure OpenAI secret and environment variable binding during deployment.')
param includeOpenAiSecret bool = false

@description('Application Gateway public IP or FQDN for WebSocket endpoint.')
param applicationGatewayHost string = ''

var secretsBase = [
  {
    name: 'acr-password'
    value: registryPassword
  }
]

var optionalOpenAiSecret = includeOpenAiSecret
  ? [
      {
        name: 'azure-openai-api-key'
        value: openAiApiKey
      }
    ]
  : []

var containerSecrets = concat(secretsBase, optionalOpenAiSecret)

var baseEnv = [
  {
    name: 'PORT'
    value: string(targetPort)
  }
  {
    name: 'AZURE_OPENAI_ENDPOINT'
    value: openAiEndpoint
  }
  {
    name: 'AZURE_OPENAI_DEPLOYMENT'
    value: openAiDeploymentName
  }
  {
    name: 'APPLICATION_GATEWAY_HOST'
    value: applicationGatewayHost
  }
]

var optionalOpenAiEnv = includeOpenAiSecret
  ? [
      {
        name: 'AZURE_OPENAI_API_KEY'
        secretRef: 'azure-openai-api-key'
      }
    ]
  : []

var containerEnv = concat(baseEnv, optionalOpenAiEnv)

var logAnalyticsName = '${prefix}-law'
var managedEnvironmentName = '${prefix}-cae'
var containerAppName = '${prefix}-ws'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    retentionInDays: 30
    features: {
      disableLocalAuth: false
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: managedEnvironmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: listKeys(logAnalytics.id, '2020-08-01').primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: containerAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: targetPort
        allowInsecure: true
        transport: 'auto'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registryServer
          username: registryUsername
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: containerSecrets
    }
    template: {
      revisionSuffix: 'blue'
      terminationGracePeriodSeconds: terminationGracePeriodSeconds
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
      containers: [
        {
          name: 'fastapi-ws'
          image: containerImage
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: targetPort
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
          env: containerEnv
        }
      ]
    }
  }
}

output containerAppName string = containerApp.name
output ingressFqdn string = containerApp.properties.configuration.ingress.fqdn
output managedEnvironmentId string = managedEnvironment.id
output logAnalyticsWorkspaceId string = logAnalytics.id
