# bash completion for box-rpg
_box_rpg() {
    local cur prev words cword
    _init_completion || return

    case "${words[1]}" in
        runtime)
            COMPREPLY=($(compgen -W 'list install remove --architecture --sdk --help' -- "$cur"))
            ;;
        config)
            COMPREPLY=($(compgen -W 'show set' -- "$cur"))
            ;;
        launch)
            COMPREPLY=($(compgen -W '--runtime --sdk --help' -- "$cur"))
            ;;
        diagnose)
            COMPREPLY=($(compgen -W '--runtime --sdk --help' -- "$cur"))
            ;;
        *)
            COMPREPLY=($(compgen -W 'inspect runtime launch config diagnose --help --version' -- "$cur"))
            ;;
    esac
}
complete -F _box_rpg box-rpg
