Online services - one .json file each (made by the Library's + Model > Service...; edit freely).

{
  "protocol": "replicate",              how the app talks to it: replicate | google (claude_code needs no file)
  "label": "Replicate",                 how the app lists it
  "key_env": "REPLICATE_API_TOKEN",     the Windows user variable that holds your key (setx NAME value)
  "settings": {"confirm_over": 1.0}     ask before a run that may cost more than this many dollars
}

The key is never stored here. Delete the file to remove the service; its online models stay.
