#!/bin/bash

SESSION="Nova"
SESSIONEXISTS=$(tmux list-sessions | grep $SESSION)

# Only create tmux session if it doesn't already exist
#if [ "$SESSIONEXISTS" = "" ]
tmux has-session -t $SESSION
if [ $? != 0 ]
then
    # Start New Session
    tmux new-session -d -s $SESSION
    tmux rename-window -t 0 'Main'

    # Create and setup windows
    tmux new-window -t $SESSION:1 -n 'novaGround'
    tmux new-window -t $SESSION:2 -n  'novaOps-back'
    tmux new-window -t $SESSION:3 -n  'novaOps-front'
    tmux new-window -t $SESSION:4 -n  'mqtt-telemetry'
    #tmux new-window -t $SESSION:4 -n  'mqtt-commands'
	
	
    # Start programs
    tmux send-keys -t 'Main' 'mosquitto_sub -h localhost -t nova/command' C-m
    
    tmux send-keys -t 'novaOps-back' 'cd novaOps-back' C-m
    #tmux send-keys -t 'novaOps-back' 'sudo docker-compose up --build' C-m
    #tmux send-keys -t 'novaOps-back' 'sudo systemctl stop docker' C-m
    tmux send-keys -t 'novaOps-back' 'bash scripts/dev_linux.sh --broker localhost' C-m

    tmux send-keys -t 'novaGround' 'cd novaGround' C-m
    tmux send-keys -t 'novaGround' './build/novaGround' C-m

    tmux send-keys -t 'novaOps-front' 'cd novaOps-front' C-m
    #tmux send-keys -t 'novaOps-front' 'npm run dev' C-m

    tmux send-keys -t 'mqtt-telemetry' 'echo "run mosquitto_sub -h localhost -t novaground/telemetry to see data stream"' C-m C-l
    #tmux send-keys -t 'mqtt-commands' 'echo "run mosquitto_sub -h localhost -t novaground/command to see command stream"' C-m C-l
    
    # Turn windows into panes
    tmux join-pane -s $SESSION:1 -t $SESSION:0
    tmux join-pane -s $SESSION:2 -t $SESSION:0
    tmux join-pane -s $SESSION:3 -t $SESSION:0
    tmux select-layout tiled
    
fi

# Attach Session, on the Main window
tmux attach-session -t $SESSION:0
