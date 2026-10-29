targetScope = 'subscription'

@minLength(1)
@maxLength(32)
param environmentName string

@description('One region supporting both Foundry and Logic Apps Automation.')
param location string

@description('Object ID of the signed-in azd deployer.')
param principalId string

var token = uniqueString(subscription().id, environmentName, location)
var tags = {
  'azd-env-name': environmentName
}

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'guessing-game'
  scope: resourceGroup
  params: {
    location: location
    token: token
    tags: tags
    principalId: principalId
  }
}

output AZURE_RESOURCE_GROUP string = resourceGroup.name
output PROJECT_ENDPOINT string = resources.outputs.projectEndpoint
output FOUNDRY_PROJECT_RESOURCE_ID string = resources.outputs.projectResourceId
output MODEL_DEPLOYMENT string = resources.outputs.modelDeployment
output AUTOMATION_PROJECT_NAME string = resources.outputs.automationProjectName
output AUTOMATION_PROJECT_RESOURCE_ID string = resources.outputs.automationProjectId
output AUTOMATION_LOCATION string = location
