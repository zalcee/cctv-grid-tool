@echo off

schtasks /create /tn "CCTVGridTool" /tr "%cd%\cgt.exe" /sc onlogon /rl highest /f