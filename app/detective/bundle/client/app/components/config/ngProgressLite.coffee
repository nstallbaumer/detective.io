angular.module('detective').config ['ngProgressLiteProvider', (ngProgressLiteProvider)->    
    ngProgressLiteProvider.settings.speed       = 300
    ngProgressLiteProvider.settings.ease        = 'ease'
    ngProgressLiteProvider.settings.trickleRate = 0.1
]