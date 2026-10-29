param location string
param token string
param tags object
param principalId string

resource foundry 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: 'foundry-${token}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'foundry-${token}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }

  resource model 'deployments' = {
    name: 'gpt-4.1'
    sku: {
      name: 'GlobalStandard'
      capacity: 30
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: 'gpt-4.1'
        version: '2025-04-14'
      }
      versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
      raiPolicyName: 'Microsoft.DefaultV2'
    }
  }

  resource project 'projects' = {
    name: 'guessing-game'
    location: location
    identity: {
      type: 'SystemAssigned'
    }
    properties: {
      displayName: 'Guessing game'
      description: 'Two prompt agents playing guess my number.'
    }
    dependsOn: [
      model
    ]
  }
}

var foundryUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')

resource deployerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry::project.id, principalId, foundryUserRole)
  scope: foundry::project
  properties: {
    roleDefinitionId: foundryUserRole
    principalId: principalId
  }
}

// The preview project PUT can be create-only; the hook creates it only when absent.
resource automation 'Microsoft.Logic/automationProjects@2026-04-01-preview' existing = {
  name: 'automation-${token}'
}

output projectEndpoint string = foundry::project.properties.endpoints['AI Foundry API']
output projectResourceId string = foundry::project.id
output modelDeployment string = foundry::model.name
output automationProjectName string = automation.name
output automationProjectId string = automation.id
