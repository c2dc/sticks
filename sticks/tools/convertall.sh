#!/bin/bash
for i in data/caldera_adversaries/0.*
	do
	      	python tools/split_campaign.py $i;
       	done
