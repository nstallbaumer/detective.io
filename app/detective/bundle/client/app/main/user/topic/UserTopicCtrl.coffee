class window.UserTopicCtrl
    # Public method to resolve
    @resolve:
        topic: ($rootScope, $stateParams, $state, $q, Common, Page, User)->
            notFound = ->
                do deferred.reject
                $state.go "404"
                deferred
            forbidden = ->
                do deferred.reject
                $state.go "403"
                deferred
            deferred = do $q.defer
            # Checks that the current topic and user exists together
            if $stateParams.topic? and $stateParams.username?
                # Activate loading mode
                Page.loading yes
                # Retreive the topic for this user
                params =
                    type: "topic"
                    slug: $stateParams.topic
                    author__username: $stateParams.username
                Common.get params, (data)=>
                    # Stop if it's an unkown topic
                    unless data.objects and data.objects.length
                        return do (if (do User.hasReadPermission) then notFound else forbidden)
                    topic = data.objects[0]
                    $state.transition.then (newState)->
                        if newState.owner and not (User.is_logged and User.owns(topic))
                            forbidden()

                    # Resolve the deffered result
                    deferred.resolve(topic)
            # Reject now
            else return notFound()
            # Return a deffered object
            deferred.promise
        individual: (Individual, $stateParams)=>
            Individual.get($stateParams).$promise
        forms: (Summary, $stateParams)=>
            Summary.cachedGet(topic: $stateParams.topic, username: $stateParams.username, id: "forms").$promise



angular.module('detective').controller 'userTopicCtrl', UserTopicCtrl